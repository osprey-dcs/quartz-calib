# Mimic the FDAS IOC - returning waveforms which correspond to (+/-) 9V with slight variance

# NOTE: Virtual DMM needs to be running before this is started

import cothread
import random
from softioc import softioc, builder
from socket import *
import sys

ideal_counts = 5560000      # Corresponds to 9Vdc in counts
adc_multiplier = 617778     # ideal_counts / 9 (counts / volt)
spread = 10000              # Maximum variance for returning quasi-noisy values (in counts); approx 16.2mV equivalent
max_array_size = 10000      # Size of waveform
num_adc_channels = 32

channel_list = []           # List of channel pv names
waveform = []
waveforms = []
pvlist = []

#dmm_ip = '192.168.1.169'
dmm_ip = '127.0.1.1'
dmm_port = 5025
cmd_read = b'READ?\n'


random.seed()

if len(sys.argv) != 2:
    print(f'{sys.argv[0]} expects 1 argument  (two-digit digitizer number)')
    exit(0)

chassis_id = sys.argv[1]

print(f'starting softIoc mimicking FDAS:{chassis_id}')

dmm_sock = socket(AF_INET, SOCK_STREAM)
dmm_sock.connect((dmm_ip, dmm_port))

for i in range(num_adc_channels):
    channel_list.append(f'Ch{i+1:02d}_')

# Set the record prefix
builder.SetDeviceName(f'FDAS:{chassis_id}:SA')

for channel in channel_list:
    waveform.clear()
    for i in range(max_array_size):
        waveform.append(ideal_counts + random.randrange(-1 * spread, spread))
    waveforms.append(builder.WaveformIn(channel, initial_value=waveform))

# Boilerplate get the IOC started
builder.LoadDatabase()
softioc.iocInit()

# Start processes required to be run after iocInit

# Update the 'voltage' values once each second.
# Monitor the virtual DVM, keeping sign of voltage waveform consistent
def periodic_update():
    global waveforms
    new_wf = []
    while True:
        dmm_sock.send(cmd_read)
        dmm_val = float(dmm_sock.recv(1024).decode().strip())
        new_wf.clear()
        if dmm_val < 0:
            # print('writing negative waveforms')
            for x in range(max_array_size):
                new_wf.append(-1*((adc_multiplier * 9) + random.randrange(-1 * spread, spread)))
            for z in range(len(waveforms)):
                waveforms[z].set(new_wf)
        else:
            # print('writing positive waveforms')
            for x in range(max_array_size):
                new_wf.append((adc_multiplier * 9) + random.randrange(-1 * spread, spread))
            for z in range(len(waveforms)):
                waveforms[z].set(new_wf)
        cothread.Sleep(1)
cothread.Spawn(periodic_update)

# Finally leave the IOC running with an interactive shell; type 'exit' to leave.
softioc.interactive_ioc(globals())
