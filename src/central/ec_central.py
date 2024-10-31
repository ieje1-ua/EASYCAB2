import time
import logging
import socket
import sys
from kafka import KafkaConsumer, KafkaProducer
from kafka.errors import KafkaError  # Asegúrate de importar KafkaError
import json
import numpy as np
from dataclasses import dataclass
from typing import Dict, Tuple
import threading


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class Taxi:
    id: int
    status: str  # 'FREE', 'BUSY', 'END'
    color: str  # 'RED' (parado) o 'GREEN' (en movimiento)
    position: Tuple[int, int]
    customer_asigned: int

@dataclass
class Location:
    id: str
    position: Tuple[int, int]
    color: str # 'BLUE' (localización) o 'YELLOW' (cliente)

class ECCentral:
    def __init__(self, kafka_bootstrap_servers, listen_port):
        self.kafka_bootstrap_servers = kafka_bootstrap_servers
        self.listen_port = listen_port
        self.producer = None
        self.consumer = None
        self.map_size = (20, 20)
        self.map = np.full(self.map_size, ' ', dtype=str)
        self.locations: Dict[str, Location] = {}
        self.taxis_file = '/data/taxis.txt'  # Ruta al fichero de taxis
        self.taxis = {}  # Guardar taxis en un atributo
        self.locations = {}
        self.map_changed = False  # Estado para detectar cambios en el mapa

    def load_map_config(self):
        try:
            with open('/data/map_config.txt', 'r') as f:
                for line in f:
                    loc_id, x, y = line.strip().split()
                    x, y = int(x), int(y)
                    self.locations[loc_id] = Location(loc_id, (x, y),"BLUE")
                    self.map[y, x] = loc_id
                    
            logger.info("Map configuration loaded successfully")
        except Exception as e:
            logger.error(f"Error loading map configuration: {e}")

    def load_taxis(self):
        """Carga los taxis desde el fichero."""
        taxis = {}
        try:
            with open(self.taxis_file, 'r') as f:
                for line in f:
                    taxi_id, status, color, pos_x, pos_y, customer_asigned = line.strip().split('#')
                    taxis[int(taxi_id)] = Taxi(
                        id=int(taxi_id),
                        position=(int(pos_x), int(pos_y)),
                        status=status,
                        color=color,
                        customer_asigned=customer_asigned
                    )
            logger.info("Taxis loaded from file")
        except Exception as e:
            logger.error(f"Error loading taxis from file: {e}")
        return taxis

    def save_taxis(self, taxis):
        """Guarda los taxis en el fichero."""
        try:
            with open(self.taxis_file, 'w') as f:
                for taxi in taxis.values():
                    f.write(f"{taxi.id}#{taxi.status}#{taxi.color}#{taxi.position[0]}#{taxi.position[1]}#{taxi.customer_asigned}\n")
            logger.info("Taxis saved to file")
            self.validate_taxis_file()
        except Exception as e:
            logger.error(f"Error saving taxis to file: {e}")

    def validate_taxis_file(self):
        """Valida el archivo de taxis para asegurarse de que no está corrupto."""
        try:
            with open(self.taxis_file, 'r') as f:
                for line in f:
                    parts = line.strip().split('#')
                    if len(parts) != 5:
                        raise ValueError(f"Invalid line in taxis file: {line}")
            logger.info("Taxis file validation successful")
        except Exception as e:
            logger.error(f"Error validating taxis file: {e}")

    def handle_taxi_auth(self, conn, addr):
        """Maneja la autenticación del taxi."""
        logger.info(f"Connection from taxi at {addr}")

        try:
            data = conn.recv(1024).decode('utf-8')
            taxi_id = int(data.strip())
            logger.info(f"Authenticating taxi with ID: {taxi_id}")
            
            # Aquí puedes implementar la lógica de autenticación
            taxis = self.load_taxis()
            if taxi_id in taxis:
                conn.sendall(b"OK")
                logger.info(f"Taxi {taxi_id} authenticated successfully.")
            else:
                conn.sendall(b"KO")
                logger.warning(f"Taxi {taxi_id} authentication failed.")
        except Exception as e:
            logger.error(f"Error during taxi authentication: {e}")
        finally:
            conn.close()
    
    def notify_customer(self, taxi):                
        self.producer.send('taxi_response', {
                'customer_id': taxi.customer_asigned,
                'status': "END",
                'assigned_taxi': taxi.id
            })
        logger.info(f"Completed trip from taxi {taxi.id} for customer {taxi.customer_asigned}")
         
    
    def update_map(self, update):
        taxi_id = update['taxi_id']
        pos_x, pos_y = update['position']
        status = update['status']
        color = update['color']
        customer_asigned = update['customer_id']

        # Actualizar estado del taxi
        taxi_updated = self.update_taxi_state(taxi_id, pos_x, pos_y, status, color, customer_asigned)
        
        # Finalizar viaje y notificar al cliente, si es necesario
        self.finalize_trip_if_needed(taxi_updated)

        # Redibujar y emitir el mapa actualizado
        self.redraw_map_and_broadcast()

    def update_taxi_state(self, taxi_id, pos_x, pos_y, status, color, customer_asigned):
        """Actualiza la información del taxi en el sistema."""
        if taxi_id in self.taxis:
            taxi = self.taxis[taxi_id]
            taxi.position = (pos_x, pos_y)
            taxi.status = status
            taxi.color = color
            taxi.customer_asigned = customer_asigned
            self.map_changed = True  # Marcar como cambiado

            return taxi
        else:
            logger.warning(f"No taxi found with id {taxi_id}")
            return None

    def finalize_trip_if_needed(self, taxi):
        """Notifica al cliente si el taxi ha finalizado el viaje."""
        if taxi and taxi.status == "END":
            self.notify_customer(taxi)

    def redraw_map_and_broadcast(self):
        """Redibuja el mapa y lo envía a todos los taxis."""
        self.draw_map()
        self.broadcast_map()


    def draw_map(self):
        """Dibuja el mapa en los logs con delimitación de bordes."""
        logger.info("Current Map State with Borders:")
        map_lines = [""]  # Agrega una línea vacía al inicio

        # Crear el borde superior
        border_row = "#" * (self.map_size[1] + 2)
        map_lines.append(border_row)

        # Limpiar el mapa primero
        self.map.fill(' ')

        # Colocar las ubicaciones en el mapa
        for location in self.locations.values():
            x, y = location.position
            self.map[y, x] = location.id

        # Colocar los taxis en el mapa
        for taxi in self.taxis.values():
            x, y = taxi.position
            self.map[y, x] = str(taxi.id)  # Usar el ID del taxi como representación

        # Crear cada fila con delimitadores laterales
        for row in self.map:
            map_lines.append("#" + "".join(row) + "#")

        # Agregar el borde inferior
        map_lines.append(border_row)
        
        # Unir las líneas y registrarlas
        logger.info("\n".join(map_lines))


    def broadcast_map(self):
        """
        Envía el estado actual del mapa a todos los taxis a través del tópico 'map_updates'.
        """
        if self.producer:
            try:
                map_data = {
                    'map': self.map.tolist(),
                    'taxis': {k: {'position': v.position, 'status': v.status, 'color': v.color} 
                                for k, v in self.taxis.items()},
                    'locations': {k: {'position': v.position, 'color': v.color}
                                    for k, v in self.locations.items()}
                }
                self.producer.send('map_updates', map_data)
                logger.info("Broadcasted map to all taxis")
            except KafkaError as e:
                logger.error(f"Error broadcasting map: {e}")

    def connect_kafka(self):
        try:
            self.producer = KafkaProducer(
                bootstrap_servers=self.kafka_bootstrap_servers,
                value_serializer=lambda v: json.dumps(v).encode('utf-8'),
                max_block_ms=5000  # Establecer timeout de 5 segundos para enviar mensajes
            )
            logger.info("Successfully connected to Kafka")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Kafka: {e}")
            return False

    def process_customer_request(self, request):
        customer_id = request['customer_id']
        destination = request['destination']
        customer_location = request['customer_location']

        # Verifica si la ubicación del cliente es válida y la agrega al mapa
        if customer_location:
            self.locations[f'customer_{customer_id}'] = Location(f'customer_{customer_id}', customer_location, 'YELLOW')
            self.map_changed = True  # Marcar como cambiado

        # Validación de la ubicación de destino
        if destination not in self.locations:
            logger.error(f"Invalid destination: {destination}")
            return False

        # Selección y asignación del taxi
        available_taxi = self.select_available_taxi()
        if available_taxi:
            self.assign_taxi_to_customer(available_taxi, customer_id, customer_location, destination)
            self.map_changed = True  # Marcar como cambiado
            return True
        else:
            logger.warning("No available taxis")
            return False

    def select_available_taxi(self):
        """Selecciona el primer taxi disponible con estado 'FREE'."""
        self.taxis = self.load_taxis()  # Asegurarse de cargar el último estado de los taxis
        return next((taxi for taxi in self.taxis.values() if taxi.status == 'FREE'), None)

    def assign_taxi_to_customer(self, taxi, customer_id, customer_location, destination):
        """Asigna el taxi al cliente y envía instrucciones."""
        taxi.status = 'BUSY'
        taxi.color = 'GREEN'
        taxi.customer_asigned = customer_id

        # Guarda el nuevo estado del taxi en el archivo y envía instrucciones
        self.save_taxis(self.taxis)
        self.notify_customer_assignment(customer_id, taxi)
        self.send_taxi_instruction(taxi, customer_id, customer_location, destination)

    def send_taxi_instruction(self, taxi, customer_id, pickup_location, destination):
        """Envía instrucciones al taxi para recoger al cliente y llevarlo al destino."""
        instruction = {
            'taxi_id': taxi.id,
            'instruction': 'MOVE',
            'pickup': self.locations[pickup_location].position,
            'destination': self.locations[destination].position,
            'customer_id': customer_id
        }
        self.producer.send('taxi_instructions', instruction)
        logger.info(f"Instructions sent to taxi {taxi.id} for customer {customer_id}")

    def notify_customer_assignment(self, customer_id, taxi):
        """Envía una respuesta al cliente confirmando la asignación del taxi."""
        response = {
            'customer_id': customer_id,
            'status': "OK",
            'assigned_taxi': taxi.id
        }
        try:
            self.producer.send('taxi_responses', response)
            self.producer.flush()
            logger.info(f"Confirmation sent to customer {customer_id}: {response}")
        except KafkaError as e:
            logger.error(f"Failed to send confirmation to customer {customer_id}: {e}")
    
    def create_consumer(self, topic):
        """Crea un consumidor de Kafka para un tópico específico."""
        return KafkaConsumer(
            topic,
            auto_offset_reset='earliest',
            bootstrap_servers=['kafka:9092'],
            group_id=f"{topic}_listener_group"
        )

    def kafka_listener_taxi_requests(self):
        consumer = self.create_consumer('taxi_requests')
        while True:
            try:
                for message in consumer:
                    if message.topic == 'taxi_requests':
                        data = message.value
                        logger.info(f"Received message on topic 'taxi_requests': {data}")
                        self.process_customer_request(data)

            except KafkaError as e:
                logger.error(f"Kafka listener error: {e}")
                self.connect_kafka()  # Reintentar la conexión
                time.sleep(5)
            except Exception as e:
                logger.error(f"General error in kafka_listener_taxi_requests: {e}")
                time.sleep(5)  # Evitar cierre inmediato

    def kafka_listener_taxi_updates(self):
        consumer = self.create_consumer('taxi_requests')
        while True:
            try:
                for message in consumer:
                    if message.topic == 'taxi_updates':
                        data = message.value
                        logger.info(f"Received message on topic 'taxi_updates': {data}")
                        self.update_map(data)

            except KafkaError as e:
                logger.error(f"Kafka listener error: {e}")
                self.connect_kafka()  # Reintentar la conexión
                time.sleep(5)
            except Exception as e:
                logger.error(f"General error in kafka_listener_taxi_updates: {e}")
                time.sleep(5)  # Evitar cierre inmediato

    def auto_broadcast_map(self):
        """Envía el estado del mapa solo cuando ha habido cambios."""
        while True:
            if self.map_changed:

                self.broadcast_map()
                self.map_changed = False  # Restablecer el indicador después de transmitir
            time.sleep(1)  # Espera 1 segundo antes de verificar nuevamente


    def start_server_socket(self):
        """Configura el servidor de sockets y maneja la autenticación de taxis en un hilo separado."""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.bind(('0.0.0.0', self.listen_port))
        self.server_socket.listen(5)  # Permitir hasta 5 conexiones en espera
        logger.info(f"Listening for taxi connections on port {self.listen_port}...")

        try:
            while True:
                conn, addr = self.server_socket.accept()
                # Crear un hilo para manejar la autenticación del taxi
                threading.Thread(target=self.handle_taxi_auth, args=(conn, addr), daemon=True).start()
        except Exception as e:
            logger.error(f"Error in start_server_socket: {e}")
        finally:
            if self.server_socket:
                self.server_socket.close()
                
    def close_producer(self):
        """Cierra el productor de Kafka con timeout."""
        if self.producer:
            try:
                # Forzar el cierre del productor con un timeout
                self.producer.close(timeout=5.0)  # 5 segundos para cerrar
                logger.info("Kafka producer closed successfully.")
            except KafkaError as e:
                logger.error(f"Error closing Kafka producer: {e}")
            except Exception as e:
                logger.error(f"General error while closing Kafka producer: {e}")


    def run(self):
        if not self.connect_kafka():
            return

        self.load_map_config()
        self.load_taxis()
        logger.info("EC_Central is running...")
        
        self.draw_map()  # Dibujar el mapa inicial


        # Iniciar el servidor de autenticación de taxis en un hilo separado
        auth_thread = threading.Thread(target=self.start_server_socket, daemon=True)
        auth_thread.start()
        
        # Iniciar el hilo para escuchar mensajes Kafka
        threading.Thread(target=self.kafka_listener_taxi_requests, daemon=True).start()
        threading.Thread(target=self.kafka_listener_taxi_updates, daemon=True).start()
        # Iniciar el hilo para la visualización del mapa
        map_broadcast_thread = threading.Thread(target=self.auto_broadcast_map, daemon=True)
        map_broadcast_thread.start()
        
        try:
            # Código de ejecución principal
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            self.close_producer()
            if self.consumer:
                self.consumer.close()
                logger.info("Kafka consumer closed.")


            
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python ec_central.py <kafka_bootstrap_servers> <listen_port>")
        sys.exit(1)

    kafka_bootstrap_servers = sys.argv[1]
    listen_port = int(sys.argv[2])
    central = ECCentral(kafka_bootstrap_servers, listen_port)
    central.run()
