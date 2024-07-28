# Mimic the Agilent precision DMM and function generator as used to excite the calibration voltages
import socket
import threading
import random


dmm_port = 5025
afg_port = 5026
random.seed()
sign = 1

def handle_client(client_socket, client_address, dtype):
    global sign
    try:
        print(f'[{dtype}] : Connection initiated from {client_address}')

        while True:
            data = client_socket.recv(1024)
            message = data.decode('utf-8')
            if not data:
                break
            print(f'[{dtype}] : Received data from {client_address}: {data.decode("utf-8")[:-1]}')
            if message[:5] == '*IDN?':
                if dtype == 'dmm':
                    ret_msg = b'Virt-a-co,DMM,1100,123abc\n'
                else:
                    ret_msg = b'Virt-a-co,AFG,2200,890xyz\n'
                client_socket.sendall(ret_msg)
            elif message[:5] == 'READ?':
                v_ret = sign * (9.0 + random.uniform(-0.0045, 0.0045))
                print(f'[{dtype}] : returning voltage: {v_ret}\n--------------')
                if dtype == 'dmm':
                    ret_msg = bytes(str(v_ret), 'utf-8')
                else:
                    ret_msg = b'0\n'
                client_socket.sendall(ret_msg)
            elif message[:5] == 'APPL:':
                if message[-5:-1] == '-4.5':
                    print(f'[{dtype}] : Switching sign: negative')
                    sign = -1
                else:
                    print(f'[{dtype}] : Switching sign: positive')
                    sign = 1


    except Exception as e:
        print(f"Exception occurred with {client_address}: {str(e)}")

    finally:
        client_socket.close()
        print(f'[{dtype}] : Connection with {client_address} closed')


def start_dmm_server(host, port):
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind((host, port))
    server_socket.listen(5)
    print(f'DMM Server listening on {host}:{port}')

    while True:
        client_socket, client_address = server_socket.accept()
        client_handler = threading.Thread(target=handle_client, args=(client_socket, client_address, 'dmm'))
        client_handler.start()


def start_afg_server(host, port):
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.bind((host, port))
    server_socket.listen(5)
    print(f'AFG Server listening on {host}:{port}')

    while True:
        client_socket, client_address = server_socket.accept()
        client_handler = threading.Thread(target=handle_client, args=(client_socket, client_address, 'afg'))
        client_handler.start()


# Example usage: Replace 'localhost' and 12345 with your desired host and ports
if __name__ == "__main__":
    my_ip = socket.gethostbyname(socket.gethostname())

    # Start two servers on different ports
    threading.Thread(target=start_dmm_server, args=(my_ip, dmm_port)).start()
    threading.Thread(target=start_afg_server, args=(my_ip, afg_port)).start()
