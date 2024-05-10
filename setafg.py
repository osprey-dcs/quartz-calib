from datetime import date, datetime
from socket import *
import sys
import time


dmm_ip = '192.168.83.47'
dmm_port = 5025
afg_ip = '192.168.83.48'
afg_port = 5025

cmd_idn = b'*IDN?\n'
cmd_read = b'READ?\n'

# For reasons unexplained at this point, the function generator outputs a DC
# voltage twice of that which is commanded.
cmd_set = b'APPL:DC DEF, DEF, '    # 
cmd_sin = b'APPL:SIN 100, 9, 0\n'    # 

# arg[1] = voltage

if len(sys.argv) != 2:
    print(f'{sys.argv[0]} expects 1 argument')
    exit(0)

arg1 = sys.argv[1]

dmm_sock = socket(AF_INET, SOCK_STREAM)
dmm_sock.connect((dmm_ip, dmm_port))
afg_sock = socket(AF_INET, SOCK_STREAM)
afg_sock.connect((afg_ip, afg_port))

# Grab DMM info
dmm_sock.send(cmd_idn)
msg = dmm_sock.recv(1024).decode().strip().split(',')
dmm_mfr = msg[0]
dmm_mdl = msg[1]
dmm_sn = msg[2]
dmm_fw = msg[3]
print(f'dmm info : {dmm_mfr} | {dmm_mdl} | {dmm_sn} | {dmm_fw}')

if arg1 == 's' or arg1 == 'S':
    afg_sock.send(cmd_sin)
else:
    desired_voltage = float(arg1)/2
    voltage_set = cmd_set + bytes(str(desired_voltage).encode()) + b'\n'
    afg_sock.send(voltage_set)

