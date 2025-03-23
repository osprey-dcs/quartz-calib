import argparse
import cothread
#from cothread.catools import *
from p4p.client.cothread import Context
from datetime import date, datetime
import math
import numpy
import socket
from softioc import softioc, builder, alarm
import sys
import time
import logging

_log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)
log_info = False

# ATF equipment
dmm_addr = ('192.168.83.47', 5025)
afg_addr = ('192.168.83.48', 5025)
# Development equipment
#dmm_addr = ('192.168.79.97', 5025)
#afg_addr = ('192.168.79.98', 5025)

num_adc_channels = 32

ctxt = Context(nt=False)  # PVA client


def pvreport(time_pv, status_pv, message):
    global log_info

    if log_info:
        _log.info('%s', message)
    current_date = date.today()
    current_time = datetime.now().strftime('%H:%M:%S')
    time_pv.set(f'{current_date} {current_time}')
    status_pv.set(message[:39])


def runcalibration(chassis_id, push_data, time_pv, message_pv, pv_status_leds):
    cmd_idn = b'*IDN?\n'
    cmd_read = b'READ?\n'
    cmd_set_pos = b'APPL:DC DEF, DEF, 9.0\n'  # +9V
    cmd_set_neg = b'APPL:DC DEF, DEF, -9.0\n'  # -9V
    cmd_set_zero = b'APPL:DC DEF, DEF, 0\n'  # 0V
    cmd_query_state = b'APPL?\n'
    cmd_set_highz = b'OUTPUT1:LOAD INF\n'

    dmm_deadband = 0.01
    calib_date = date.today()
    calib_time = datetime.now().strftime('%H:%M:%S')
    file_time = datetime.now().strftime('%H-%M-%S')

    overall_pass = True  # Goes False if any channel fails calibration

    expected_count = 5560000  # Approx count value for 9v input
    count_tolerance = 10000  # number of counts difference we tolerate
    offset_tolerance = .01  # largest offset we tolerate (we expect zero as offset)
    expected_slope = 1.6e-6  # Expected slope values based on previous runs
    slope_tolerance = 1e-6  # largest slope variation we tolerate
    settle_slope = 1e-3  # Largest slope magnitude we can tolerate for settling purposes

    outfile = f'{calib_date}_{file_time}_cal_{chassis_id}_bipolar'
    dmm_sock = socket.create_connection(dmm_addr)
    dmm_rx = dmm_sock.makefile('rb', buffering=0)
    afg_sock = socket.create_connection(afg_addr)
    afg_rx = afg_sock.makefile('rb', buffering=0)

    pv_col1_message.set(f'Slope < {settle_slope}')
    pv_col2_message.set(f'Cnts < {count_tolerance}')
    for pv in pv_status_leds:
        pv.set(1)
    for pv in pv_ch_slope:
        pv.set(99999)
    for pv in pv_ch_mean:
        pv.set(99999)

    # Grab DMM info
    dmm_sock.sendall(cmd_idn)
    msg = dmm_rx.readline(1024).decode().strip()
    pvreport(time_pv, message_pv, f'DMM info : {msg}')
    dmm_mfr, dmm_mdl, dmm_sn, dmm_fw = msg.split(',')[:4]

    # grab AFG info
    afg_sock.sendall(cmd_idn)
    msg = afg_rx.readline(1024)
    pvreport(time_pv, message_pv, f'AFG info : {msg.decode().strip()}')

    # Zero AFG and set to high Z output
    afg_sock.sendall(cmd_set_zero)
    afg_sock.sendall(cmd_set_highz)

    calib_pairs = []

    try:
        r_file = open(f'{outfile}_raw.csv', 'w')
    except Exception as e:
        raise RuntimeError(f'Error writing file: {e}')

    r_file.write(f'[BIPOLAR MODE] Calibrating chassis: {chassis_id}\n')
    r_file.write(f'Date and time: {calib_date} {calib_time}\n')
    r_file.write(f'DMM Model: {dmm_mfr} {dmm_mdl}\n')
    r_file.write(f'DMM serial number: {dmm_sn}\n')
    r_file.write(f'DMM firmware ver : {dmm_fw}\n')
    r_file.write('ch, volts, waveform\n')

    # Set to positive voltage
    pvreport(time_pv, message_pv, 'Sourcing positive voltage')
    afg_sock.sendall(cmd_set_pos)  # Set function generator to positive voltage
    cothread.Sleep(.5)
    afg_sock.sendall(cmd_query_state)
    msg = afg_rx.readline(1024)
    if (msg.decode().split(' ')[0].strip('"') != 'DC' or
            msg.decode().split(' ')[1].split(',')[2].strip('"\n')[:2] != '+9'):
        pvreport(time_pv, message_pv, 'Unable to verify AFG set to +9vDC')
        raise RuntimeError(f'Unable to set AFG to +9V')

    # Wait for adc to settle at desired range
    def settle(upper):
        noisy_chs = []
        grp = ctxt.get('FDAS:ACQ:rate')
        # Find acquisition rate in kHz
        daq_rate = grp.value['choices'][grp.value['index']].split(' ')[0]
        # Fit a line to determine a scaled settling time
        settle_delay = 11 / int(daq_rate)       # Acquisition time for 10k points based on the ADC rate
        pvname = f'FDAS:{chassis_id}:SA:V'
        grp = ctxt.get(pvname)
        xarr = numpy.arange(len(grp.value[f'Ch01'][:, None]))  # Bin numbers for fitting a line per channel
        for n in range(10):  # Allow 10 chances to converge
            pvreport(time_pv, message_pv, f'Acquiring for ~{settle_delay} seconds')
            cothread.Sleep(settle_delay)
            grp = ctxt.get(pvname)
            traces = numpy.concatenate(
                [grp.value[f'Ch{ch + 1:02d}'][:, None] for ch in range(num_adc_channels)],
                axis=1
            )
            assert traces.shape == (10000, 32), traces.shape
            pvreport(time_pv, message_pv, f'Checking slope and counts')
            for ch in range(num_adc_channels):
                slope = numpy.polyfit(xarr, grp.value[f'Ch{ch + 1:02d}'][:, None], 1)[0][0].item()
                pv_ch_slope[ch].set(slope)
                pv_ch_mean[ch].set(abs(abs(traces.mean()) - expected_count))
                if abs(slope) > upper:
                    pvreport(time_pv, message_pv, f'Slope error: Ch{ch + 1:02d} / {slope}')
                    if ch + 1 not in noisy_chs:
                        pv_status_leds[ch].set(2)
                        noisy_chs.append(ch + 1)
                elif abs(abs(traces.mean()) - expected_count) > count_tolerance:
                    pvreport(time_pv, message_pv, f'ADC count error: Ch{ch + 1:02d} / {traces.mean()}')
                    if ch + 1 not in noisy_chs:
                        pv_status_leds[ch].set(2)
                        noisy_chs.append(ch + 1)
                else:
                    if ch + 1 in noisy_chs:
                        pv_status_leds[ch].set(1)
                        noisy_chs.remove(ch + 1)
            if len(noisy_chs) == 0:
                return traces
        pvreport(time_pv, message_pv, f'Channels {noisy_chs} did not settle')
        raise RuntimeError(f'ADC does not settle: channels {noisy_chs}.  Unable to proceed')
    ch_pos = settle(settle_slope)

    def dmm_read():
        vals = [None, None]
        for i in range(2):
            dmm_sock.sendall(cmd_read)
            vals[i] = float(dmm_rx.readline(1024).decode().strip())
            if not math.isfinite(vals[i]):
                pvreport(time_pv, message_pv, f'**ABORT** error reading DMM: {chassis_id}')
                raise RuntimeError('DMM readout error.  Unable to proceed')

        if numpy.abs(numpy.diff(vals))[0] > dmm_deadband:
            raise RuntimeError('DMM does not settle.  Unable to proceed')
        return vals[1]

    dmm_p = dmm_read()

    for ch_id in range(num_adc_channels):
        pvreport(time_pv, message_pv, f'Recording ch {ch_id + 1} positive voltage')
        pv_status_leds[ch_id].set(3)

    pvreport(time_pv, message_pv, 'Sourcing negative voltage')

    for pv in pv_status_leds:
        pv.set(1)
    # Switch to negative
    afg_sock.sendall(cmd_set_neg)  # Set function generator to negative voltage
    cothread.Sleep(.5)
    afg_sock.sendall(cmd_query_state)
    msg = afg_rx.readline(1024)
    if (msg.decode().split(' ')[0].strip('"') != 'DC' or
            msg.decode().split(' ')[1].split(',')[2].strip('"\n')[:2] != '-9'):
        pvreport(time_pv, message_pv, 'Unable to verify AFG set to -9vDC')
        raise RuntimeError(f'Unable to set AFG to -9V')
    ch_neg = settle(settle_slope)
    dmm_n = dmm_read()

    for ch_id in range(num_adc_channels):
        pvreport(time_pv, message_pv, f'Recording ch {ch_id + 1} negative voltage')
        pv_status_leds[ch_id].set(3)

    pvreport(time_pv, message_pv, 'Calculating slope and offset')
    for pv in pv_status_leds:
        pv.set(1)

    # Calculate linear fit for each channel
    x1 = ch_pos.mean(0)  # [nChan]
    x2 = ch_neg.mean(0)
    y1 = dmm_p
    y2 = dmm_n

    pv_col1_message.set(f'Calc slope')
    pv_col2_message.set(f'Calc offset')

    for ch in range(num_adc_channels):
        # Calculate slope and intercept
        slope, intercept = numpy.polyfit([x1[ch], x2[ch]], [y1, y2], 1)
        pv_ch_slope[ch].set(slope)
        pv_ch_mean[ch].set(intercept)

        ch_pass = 'Pass'

        # Boundary checks
        if abs(float(x1[ch])) - expected_count > count_tolerance:
            pvreport(time_pv, message_pv, f'adc_wave_p boundary violation')
            ch_pass = 'FAIL'
            pv_status_leds[ch].set(2)
        elif abs(float(x2[ch])) - expected_count > count_tolerance:
            pvreport(time_pv, message_pv, f'adc_wave_n boundary violation')
            ch_pass = 'FAIL'
            pv_status_leds[ch].set(2)
        elif abs(intercept) > offset_tolerance:
            pvreport(time_pv, message_pv, f'intercept boundary violation = {intercept}')
            ch_pass = 'FAIL'
            pv_status_leds[ch].set(2)
        elif abs(slope - expected_slope) > slope_tolerance:
            pvreport(time_pv, message_pv, f'slope boundary violation = {slope}')
            ch_pass = 'FAIL'
            pv_status_leds[ch].set(2)
        else:
            pv_status_leds[ch].set(3)

        if push_data:  # User requested pushing data to PVs
            pvreport(time_pv, message_pv, 'Updating slope and offset on PVs')
            cothread.Sleep(.1)
            ctxt.put(f'FDAS:{chassis_id}:SA:Ch{ch + 1:02d}:ASLO', slope)
            ctxt.put(f'FDAS:{chassis_id}:SA:Ch{ch + 1:02d}:AOFF', intercept)
            # Write time-stamp to PV for last calibration, or zero if calibration failed
            if ch_pass != 'FAIL':
                ctxt.put(f'FDAS:{chassis_id}:SA:Ch{ch + 1:02d}:TCAL', time.time())
            else:
                ctxt.put(f'FDAS:{chassis_id}:SA:Ch{ch + 1:02d}:TCAL', 0)

        calib_pairs.append(
            (dmm_p, dmm_n, float(x1[ch]), float(x2[ch]), slope, intercept, ch_pass))
        r_file.write(f'{ch + 1}, {dmm_p}')
        for m in ch_pos[:, ch]:
            r_file.write(f', {m}')
        r_file.write('\n')
        r_file.write(f'{ch + 1}, {dmm_n}')
        for m in ch_neg[:, ch]:
            r_file.write(f', {m}')
        r_file.write('\n')

        if ch_pass != 'Pass':
            overall_pass = False
    r_file.close()

    # Write summary file
    with open(f'{outfile}_calc.csv', 'w') as o_file:
        o_file.write(f'[BIPOLAR MODE] Calibrating chassis: {chassis_id}\n')
        o_file.write(f'Date and time: {calib_date} {calib_time}\n')
        o_file.write(f'DMM Model: {dmm_mfr} {dmm_mdl}\n')
        o_file.write(f'DMM serial number: {dmm_sn}\n')
        o_file.write(f'DMM firmware ver : {dmm_fw}\n')
        o_file.write( 'ch, volts_1, volts_2, counts_1, counts_2,  slope, offset, result\n')
        i = 0
        for elem in calib_pairs:
            i += 1
            o_file.write(f'{i}')
            for member in elem:
                o_file.write(f',{member}')
            o_file.write('\n')
    o_file.close()
    afg_sock.sendall(cmd_set_zero)

    if overall_pass:
        return 'Success'
    else:
        return 'Failed'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='ATF Calibration IOC')
    parser.add_argument('-i', '--info',  action='store_true', help='Display info log messages on console')
    parser.add_argument('-f', '--faddr', help=f'Specify function generator IP addr [{afg_addr[0]}]')
    parser.add_argument('-fp', '--fport', help=f'Specify function generator SCPI port [{afg_addr[1]}]')
    parser.add_argument('-v', '--vaddr', help=f'Specify voltmeter IP addr [{dmm_addr[0]}]')
    parser.add_argument('-vp', '--vport', help=f'Specify voltmeter SCPI port [{dmm_addr[1]}]')
    args = parser.parse_args()

    log_info = args.info

    if args.faddr:
        afg_ip = args.faddr
    else:
        afg_ip = afg_addr[0]
    if args.fport:
        afg_port = args.fport
    else:
        afg_port = afg_addr[1]

    if args.vaddr:
        dmm_ip = args.vaddr
    else:
        dmm_ip = dmm_addr[0]
    if args.vport:
        dmm_port = args.vport
    else:
        dmm_port = dmm_addr[1]

    afg_addr = (afg_ip, afg_port)
    dmm_addr = (dmm_ip, dmm_port)

    # Set up pvs
    init_date = date.today()
    init_time = datetime.now().strftime('%H:%M:%S')
    builder.SetDeviceName('FDAS:Calib')
    pv_status_leds = []
    pv_ch_slope = []
    pv_ch_mean = []
    pv_chassis_id = builder.aOut('chassisID', initial_value=1)
    pv_start_calibration = builder.boolOut('start', initial_value=0)
    pv_status_timedate = builder.stringOut('status_time', initial_value=f'{init_date} {init_time}')
    pv_status_message = builder.stringOut('status_message', initial_value='IOC startup')
    pv_col1_message = builder.stringOut('col1_message', initial_value='IOC startup')
    pv_col2_message = builder.stringOut('col2_message', initial_value='IOC startup')

    pv_test_mode = builder.boolOut('testmode', initial_value=1)
    for i in range(num_adc_channels):
        # 0 = unk, 1 = calibrating, 2 = fail, 3 = pass
        pv_status_leds.append(builder.aIn(f'stat{i + 1}', initial_value=0))
        pv_ch_slope.append(builder.aIn(f'ch{i+1}:slope', initial_value=99999))
        pv_ch_mean.append(builder.aIn(f'ch{i+1}:mean', initial_value=0))

    # set up the ioc
    builder.LoadDatabase()
    softioc.iocInit()


    def update():
        for pv in pv_status_leds:
            pv.set(0)
        while True:
            if pv_start_calibration.get() != 0:
                try:
                    calib_date = date.today()
                    chassis_id = int(pv_chassis_id.get())
                    pvreport(pv_status_timedate, pv_status_message, f'Beginning cal of chassis {chassis_id}')
                    push_data = pv_test_mode.get() != 1
                    rval = runcalibration(f'{chassis_id:02}', push_data, pv_status_timedate, pv_status_message,
                                          pv_status_leds)
                    end_time = datetime.now().strftime('%H:%M:%S')
                    pv_status_timedate.set(f'{calib_date} {end_time}')
                    pvreport(pv_status_timedate, pv_status_message,
                             f'Chassis {chassis_id:02} calibration result : {rval}')
                except Exception as e:
                    pvreport(pv_status_timedate, pv_status_message,
                             f'Chassis {chassis_id:02} exc : {e}')
                    _log.exception('Unhandled exception: %s', e)
                finally:
                    pv_start_calibration.set(0)
            cothread.Sleep(1)


    cothread.Spawn(update)
    try:
        softioc.interactive_ioc(globals())

    except Exception as e:
        _log.exception('Unhandled exception: %s', e)
        exit(-1)
