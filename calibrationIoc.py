import cothread
#from cothread.catools import *
from p4p.client.cothread import Context
from datetime import date, datetime
import math
import numpy
from socket import *
from softioc import softioc, builder, alarm
import sys
import time
import logging

_log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ATF equipment
dmm_addr = ('192.168.83.47', 5025)
afg_addr = ('192.168.83.48', 5025)
# Development equipment
#dmm_addr = ('192.168.79.97', 5025)
#afg_addr = ('192.168.79.98', 5025)

num_adc_channels = 32

ctxt = Context(nt=False) # PVA client

def pvreport(time_pv, status_pv, message):
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

    outfile = f'{calib_date}_{file_time}_cal_{chassis_id}_bipolar'
    dmm_sock = create_connection(dmm_addr)
    dmm_rx = dmm_sock.makefile('rb', buffering=0)
    afg_sock = create_connection(afg_addr)
    afg_rx = afg_sock.makefile('rb', buffering=0)

    for pv in pv_status_leds:
        pv.set(1)

    # Grab DMM info
    dmm_sock.sendall(cmd_idn)
    # eg. "Agilent Technologies,34410A,MY47024415,2.35-2.35-0.09-46-09"
    msg = dmm_rx.readline(1024).decode().strip().split(',')
    dmm_mfr, dmm_mdl, dmm_sn, dmm_fw = msg[:4]

    # grab AFG info
    afg_sock.sendall(cmd_idn)
    msg = afg_rx.readline(1024)
    #print(f'afg info : {msg.decode().strip()}')

    # Set AFG to positive voltage
    afg_sock.sendall(cmd_set_pos)
#    cothread.Sleep(.5)

    dmm_list = []
    adc_list = []
    calib_pairs = []
    ch_pos = []
    ch_neg = []
    dmm_pos = []
    dmm_neg = []

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
    # print('Switching to positive excitation voltage')
    afg_sock.sendall(cmd_set_pos)  # Set function generator to positive voltage
    # TODO: query to confirm setting applied

    # Wait until adc to settle at desired range
    def settle(lower, upper):
        assert lower < upper, (lower, upper)
        pvname = f'FDAS:{chassis_id}:SA:V'
        for n in range(10):
            cothread.Sleep(0.5)
            # fetch all traces
            grp = ctxt.get(pvname)
            traces = numpy.concatenate(
                [grp.value[f'Ch{ch+1:02d}'][:,None] for ch in range(num_adc_channels)],
                axis=1
            )
            assert traces.shape==(10000, 32), traces.shape
            pvreport(time_pv, message_pv, f'Settle {n} {traces.min()} {traces.mean()} {traces.std(0).max()} {traces.max()}')
            if traces.std(0).max() > 38: # observed noise level ~30 counts
                continue # TODO: better to fit each trace to a line and check slope against upper bound
            if traces.mean() > lower and traces.mean() < upper:
                return traces

        pvreport(time_pv, message_pv, '**ABORT**: ADC does not settle  - check cable')
        raise RuntimeError('ADC does not settle.  Unable to proceed')

    ch_pos = settle(6170000, 6180000)
    print('settled pos ADC', ch_pos.mean(0))

    def dmm_read():
        vals = [None, None]
        for i in range(2):
            dmm_sock.sendall(cmd_read)
            vals[i] = float(dmm_rx.readline(1024).decode().strip())
            if not math.isfinite(vals[i]):
                pvreport(time_pv, message_pv,f'**ABORT** error reading DMM: {chassis_id}')
                raise RuntimeError('DMM readout error.  Unable to proceed')

        if numpy.abs(numpy.diff(vals))[0] > dmm_deadband:
            raise RuntimeError('DMM does not settle.  Unable to proceed')
        return vals[1]

    dmm_p = dmm_read()
    print('settled pos DMM', dmm_p)

    for ch_id in range(num_adc_channels):
        pvreport(time_pv, message_pv, f'Recording ch {ch_id+1} positive voltage')
        pv_status_leds[ch_id].set(3)

    pvreport(time_pv, message_pv, 'Sourcing negative voltage')

    for pv in pv_status_leds:
        pv.set(1)
    ## Switch to negative
    afg_sock.sendall(cmd_set_neg)  # Set function generator to negative voltage
    ch_neg = settle(-6180000, -6170000)
    print('settled neg ADC', ch_neg.mean(0))

    dmm_n = dmm_read()
    print('settled neg DMM', dmm_n)

    for ch in range(num_adc_channels):
        pvreport(time_pv, message_pv, f'Channel {ch_id+1} negative voltage acquired')
        pv_status_leds[ch_id].set(3)

    pvreport(time_pv, message_pv, 'Calculating slope and offset')
    for pv in pv_status_leds:
        pv.set(1)

    # Calculate linear fit for each channel
    x1 = ch_pos.mean(0) # [nChan]
    x2 = ch_neg.mean(0)
    y1 = dmm_p
    y2 = dmm_n

    for ch in range(num_adc_channels):
        # Calculate slope and intercept
        slope, intercept = numpy.polyfit([x1[ch], x2[ch]], [y1, y2], 1)

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
            pvreport(time_pv, message_pv,'Updating slope and offset on PVs')
            cothread.Sleep(.1)
            ####### TODO
            #caput(f'FDAS:{chassis_id}:SA:Ch{ch+1:02d}:ASLO', slope)
            #caput(f'FDAS:{chassis_id}:SA:Ch{ch+1:02d}:AOFF', intercept)
            # Write time-stamp to PV for last calibration, or zero if calibration failed
            #if ch_pass != 'FAIL':
            #    caput(f'FDAS:{chassis_id}:SA:Ch{ch+1:02d}:TCAL', time.time())
            #else:
            #    caput(f'FDAS:{chassis_id}:SA:Ch{ch+1:02d}:TCAL', 0)
        else:
            print(f'caput FDAS:{chassis_id}:SA:Ch{ch+1:02d}:ASLO', slope)
            print(f'caput FDAS:{chassis_id}:SA:Ch{ch+1:02d}:AOFF', intercept)
            print(f'caput FDAS:{chassis_id}:SA:Ch{ch+1:02d}:TCAL', time.time() if ch_pass != 'FAIL' else 0)

        calib_pairs.append(
            (dmm_p, dmm_n, float(x1[ch]), float(x2[ch]), slope, intercept, ch_pass))
        r_file.write(f'{ch + 1}, {dmm_p}')
        for m in ch_pos[:,ch]:
            r_file.write(f', {m}')
        r_file.write(f'\n')
        r_file.write(f'{ch + 1}, {dmm_n}')
        for m in ch_neg[:,ch]:
            r_file.write(f', {m}')
        r_file.write(f'\n')

        if ch_pass != 'Pass':
            overall_pass = False
    r_file.close()

    # Write summary file
    try:
        with open(f'{outfile}_calc.csv', 'w') as o_file:
            o_file.write(f'[BIPOLAR MODE] Calibrating chassis: {chassis_id}\n')
            o_file.write(f'Date and time: {calib_date} {calib_time}\n')
            o_file.write(f'DMM Model: {dmm_mfr} {dmm_mdl}\n')
            o_file.write(f'DMM serial number: {dmm_sn}\n')
            o_file.write(f'DMM firmware ver : {dmm_fw}\n')
            o_file.write(f'ch, volts_1, volts_2, counts_1, counts_2,  slope, offset, result\n')
            i = 0
            for elem in calib_pairs:
                i += 1
                o_file.write(f'{i}')
                for member in elem:
                    o_file.write(f',{member}')
                o_file.write(f'\n')
    except Exception as e:
        log.exception('Error writing file: %r', e)
    o_file.close()
    afg_sock.sendall(cmd_set_zero)

    if overall_pass:
        # print(f'Calibration of chassis {chassis_id} passed on {calib_date} at {calib_time}')
        return('Success')
    else:
        #print('***************')
        #print(f'Calibration of chassis {chassis_id} FAILED on {calib_date} at {calib_time}')
        #print('***************')
        return('Failed')


if __name__ == '__main__':
    # Set up pvs
    init_date = date.today()
    init_time = datetime.now().strftime('%H:%M:%S')
    builder.SetDeviceName('FDAS:Calib')
    pv_status_leds = []
    pv_chassis_id = builder.aOut('chassisID', initial_value=1)
    pv_start_calibration = builder.boolOut('start', initial_value=0)
    pv_status_timedate = builder.stringOut('status_time', initial_value=f'{init_date} {init_time}')
    pv_status_message = builder.stringOut('status_message', initial_value='IOC startup')
    pv_test_mode = builder.boolOut('testmode', initial_value=1)
    for i in range(32):
        # 0 = unk, 1 = calibrating, 2 = fail, 3 = pass
        pv_status_leds.append(builder.aIn(f'stat{i+1}', initial_value=0))

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
        print(f'Found exception {e} - terminating')
        exit(-1)
