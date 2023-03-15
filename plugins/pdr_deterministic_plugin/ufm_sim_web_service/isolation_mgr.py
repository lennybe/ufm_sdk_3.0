#
# Copyright © 2013-2023 NVIDIA CORPORATION & AFFILIATES. ALL RIGHTS RESERVED.
#
# This software product is a proprietary product of Nvidia Corporation and its affiliates
# (the "Company") and all right, title, and interest in and to the software
# product, including all associated intellectual property rights, are and
# shall remain exclusively with the Company.
#
# This software product is governed by the End User License Agreement
# provided with the software product.
#

from datetime import datetime
from datetime import timedelta
import time
import configparser
import pandas as pd
import json

from constants import PDRConstants as Constants
from ufm_communication_mgr import UFMCommunicator
# should actually be persistent and thread safe dictionary pf PortStates


class PortData(object):
    def __init__(self):
        self.counters_values = {}
        self.change_time = datetime.now()


class PortState(object):
    def __init__(self, name):
        self.name = name
        self.state = Constants.STATE_NORMAL # isolated | treated
        self.cause = Constants.ISSUE_INIT # oonoc, pdr, ber
        self.maybe_fixed = False
        self.change_time = datetime.now()

    def update(self, state, cause):
        self.state = state
        self.cause = cause
        self.change_time = datetime.now()
    
    def get_cause(self):
        return self.cause
    
    def get_state(self):
        return self.state
    
    def get_change_time(self):
        return self.change_time
        
class Issue(object):
    def __init__(self, port, cause):
        self.cause = cause
        self.port = port

class IsolationMgr:
    
    def __init__(self, ufm_client: UFMCommunicator, logger):
        self.ufm_client = ufm_client
        # {port_name: PortState}
        self.ports_states = dict()
        # {port_name: telemetry + speed}
        self.ports_data = dict()
        self.ufm_latest_isolation_state = []
        
        pdr_config = configparser.ConfigParser()
        pdr_config.read(Constants.CONF_FILE)
        
        # Take from Conf
        self.t_isolate = pdr_config.getint(Constants.CONF_COMMON, Constants.T_ISOLATE)
        self.max_num_isolate = pdr_config.getint(Constants.CONF_COMMON, Constants.MAX_NUM_ISOLATE)
        self.tmax = pdr_config.getint(Constants.CONF_COMMON, Constants.TMAX)
        self.d_tmax = pdr_config.getint(Constants.CONF_COMMON, Constants.D_TMAX)
        self.max_pdr = pdr_config.getfloat(Constants.CONF_COMMON, Constants.MAX_PDR)
        self.configured_ber_check = pdr_config.getboolean(Constants.CONF_COMMON,Constants.CONFIGURED_BER_CHECK)
        self.dry_run = pdr_config.getboolean(Constants.CONF_COMMON,Constants.DRY_RUN)
        self.do_deisolate = pdr_config.getboolean(Constants.CONF_COMMON,Constants.DO_DEISOLATION)
        self.deisolate_consider_time = pdr_config.getint(Constants.CONF_COMMON,Constants.DEISOLATE_CONSIDER_TIME)
        self.automatic_deisolate = pdr_config.getboolean(Constants.CONF_COMMON,Constants.AUTOMATIC_DEISOLATE)
        # Take from Conf
        self.logger = logger
        self.isolation_matrix = pd.read_csv(Constants.BER_MATRIX)
        min_threshold = self.isolation_matrix[[
            Constants.CSV_RAW_BER_ISOLATE, Constants.CSV_RAW_BER_DEISOLATE,
            Constants.CSV_EFF_BER_ISOLATE, Constants.CSV_EFF_BER_DEISOLATE, 
            Constants.CSV_SYMBOL_BER_ISOLATE, Constants.CSV_SYMBOL_BER_DEISOLATE]].min()
        self.ber_wait_time = self.calc_max_ber_wait_time(min_threshold)
        self.start_time = time.time()
        self.ber_tele_data = pd.DataFrame(columns=[Constants.TIMESTAMP, Constants.RAW_BER, Constants.EFF_BER, Constants.SYMBOL_BER])
        self.speed_types = {
            "FDR": 16,
            "EDR": 32,
            "HDR": 64,
            "NDR": 128,
            }
                
        # DEBUG
        # self.iteration = 0
        # self.deisolate_consider_time = 0
        # self.d_tmax = 9000
        
    def calc_max_ber_wait_time(self, min_threshold):
        # min speed EDR = 32 Gb/s
        min_speed, min_width = 32 * 1024 * 1024 * 1024, 1
        min_port_rate = max_speed * max_width
        min_bits = float(format(float(min_threshold), '.0e').replace('-', ''))
        min_sec_to_wait = min_bits / min_port_rate
        return min_sec_to_wait
    def is_out_of_operating_conf(self, port_name):
        port_telemetry = self.ports_data.get(port_name)
        if not port_telemetry:
            return
        temp = port_telemetry.get(Constants.TEMP_COUNTER)
        if temp and temp > self.tmax:
            return True
        return False

    def eval_isolation(self, port_name, cause):
        self.logger.info("Evaluating isolation of port {0} with cause {1}".format(port_name, cause))
        if port_name in self.ufm_latest_isolation_state:
            self.logger.info("Port is already isolated. skipping...")
            return

        # if out of operating conditions we ignore the cause
        if self.is_out_of_operating_conf(port_name):
            cause = Constants.ISSUE_OONOC
        
        if not self.dry_run:
            ret = self.ufm_client.isolate_port(port_name)
            if not ret or ret.status_code != 200:
                self.logger.warning("Failed isolating port: %s with cause: %s... status_code= %s", port_name, cause, ret.status_code)        
                return
        port_state = self.ports_states.get(port_name)
        if not port_state:
            self.ports_states[port_name] = PortState(port_name)
        self.ports_states[port_name].update(Constants.STATE_ISOLATED, cause)
            
        self.logger.warning("Isolated port: %s cause: %s", port_name, cause)

    def eval_deisolate(self, port_name):
        if not port_name in self.ufm_latest_isolation_state:
            if self.ports_states.get(port_name):
                self.ports_states.pop(port_name)
            return

        # we dont return those out of NOC
        if self.is_out_of_operating_conf(port_name):
            cause = Constants.ISSUE_OONOC
            self.ports_states[port_name].update(Constants.STATE_ISOLATED, cause)
            return

        # we need some time after the change in state
        elif datetime.now() >= self.ports_states[port_name].get_change_time() + timedelta(minutes=self.deisolate_consider_time):
            # TODO: handle BER
            if self.check_ber_threshold(port_name, port_speed, asic, fec_mode, raw_ber_val, eff_ber_val, symbol_ber_val, isolation=False):
                cause = Constants.ISSUE_BER
                self.ports_states[port_name].update(Constants.STATE_ISOLATED, cause)
                return
        else:
            # too close to state change
            return
        
        # port is clean now - de-isolate it
        # using UFM "mark as healthy" API - PUT /ufmRestV2/app/unhealthy_ports 
            # {
            # "ports": [
            #     "e41d2d0300062380_3"
            # ],
            # "ports_policy": "HEALTHY"
            # }
        if not self.dry_run:
            ret = self.ufm_client.deisolate_port(port_name)
            if not ret or ret.status_code != 200:
                self.logger.warning("Failed deisolating port: %s with cause: %s... status_code= %s", port_name, self.ports_states[port_name].cause, ret.status_code)        
                return
        self.ports_states.pop(port_name)
        self.logger.warning("Deisolated port: %s", port_name)
                
    def get_rate_and_update(self, port_name, counter_name, new_val):
        port_data = self.ports_data.get(port_name)
        if port_data:
            old_val = port_data.get(counter_name)
            if old_val and new_val > old_val:
                counter_delta = (old_val - new_val) / self.t_isolate
            else:
                counter_delta = 0
        else:
            self.ports_data[port_name] = {}
            counter_delta = 0
        self.ports_data.get(port_name)[counter_name] = new_val
        return counter_delta

    def read_next_set_of_high_ber_or_pdr_ports(self):
        issues = {}
        ports_counters = self.ufm_client.get_telemetry()
        if not ports_counters:
            self.logger.error("Couldn't retrieve telemetry data")
            return issues
        ports_counters = list(ports_counters.values())[0]
        for port_name, statistics in ports_counters.get("Ports").items():
            # if not self.ports_states.get(port_name):
            #     self.ports_states[port_name] = PortState(port_name)
            counters = statistics.get('statistics')
            errors = counters.get(Constants.RCV_ERRORS_COUNTER, 0) + counters.get(Constants.RCV_REMOTE_PHY_ERROR_COUNTER, 0) 
            error_rate = self.get_rate_and_update(port_name, Constants.ERRORS_COUNTER, errors)
            rcv_pkts = counters.get(Constants.RCV_PACKETS_COUNTER, 0)
            rcv_pkt_rate = self.get_rate_and_update(port_name, Constants.RCV_PACKETS_COUNTER, rcv_pkts)
            cable_temp = counters.get(Constants.TEMP_COUNTER)
            # DEBUG
            # if port_name == "e41d2d0300062380_3":
            #     self.iteration += 1
            #     if self.iteration < 3:
            #         cable_temp = 90
            #     else:
            #         cable_temp = 30
            if cable_temp is not None:
                dT = abs(self.ports_data[port_name].get(Constants.TEMP_COUNTER, 0) - cable_temp)
                self.ports_data[port_name][Constants.TEMP_COUNTER] = cable_temp
            if rcv_pkt_rate and error_rate / rcv_pkt_rate > self.max_pdr:
                issues[port_name] = Issue(port_name, Constants.ISSUE_PDR)
            elif cable_temp and (cable_temp > self.tmax or dT > self.d_tmax):
                issues[port_name] = Issue(port_name, Constants.ISSUE_OONOC)
            if self.configured_ber_check:
                #TODO calc BER
                symbol_ber_val = counters.get(Constants.SYMBOL_BER)
                eff_ber_val = counters.get(Constants.EFF_BER)
                raw_ber_val = counters.get(Constants.RAW_BER)
                #TODO calc BER
                if symbol_ber_val or eff_ber_val or raw_ber_val:
                    timestamp = time.time()
                    ber_data = {
                        Constants.TIMESTAMP : timestamp,
                        Constants.RAW_BER : raw_ber_val,
                        Constants.EFF_BER : eff_ber_val,
                        Constants.SYMBOL_BER : symbol_ber_val
                    }
                    self.ber_tele_data = self.ber_tele_data.append(ber_data, ignore_index=True)
                    self.ber_tele_data = self.ber_tele_data[self.ber_tele_data[Constants.TIMESTAMP] > self.start_time - self.ber_wait_time + self.t_isolate * 10]
                    self.start_time = self.ber_tele_data[Constants.TIMESTAMP].min()
                    port_data = self.ports_data.get(port_name)
                    fec_mode = counters.get(Constants.FEC_MODE)
                    if fec_mode is None:
                        continue
                    port_speed = port_data.get(Constants.ACTIVE_SPEED)
                    port_asic = port_data.get(Constants.ASIC)
                    port_width = port_data.get(Constants.WIDTH)
                    if not port_speed or port_asic is None or not port_width:
                        port_speed, port_asic, port_width = self.get_port_metadata(port_name)
                        port_data[Constants.ACTIVE_SPEED] = port_speed
                        port_data[Constants.ASIC] = port_asic
                        port_data[Constants.WIDTH] = port_width

                    if timestamp - self.start_time < self.ber_wait_time:
                        continue
                    raw_ber_rate, eff_ber_rate, symbol_ber_rate = self.calc_ber_rates(port_speed, port_width, timestamp, self.start_time)
                    if self.check_ber_threshold(port_name, port_speed, port_asic, fec_mode, raw_ber_rate, eff_ber_rate, symbol_ber_rate, isolation=True):
                        issued_port = issues.get(port_name)
                        if issued_port:
                            issued_port.cause = Constants.ISSUE_PDR_BER
                        else:
                            issues[port_name] = Issue(port_name, Constants.ISSUE_BER)                    
        return issues

    def calc_single_rate(self, port_speed, port_width, min_timestamp, max_timestamp, col_name):
        min_value = self.ber_tele_data.loc[self.ber_tele_data[Constants.TIMESTAMP] == min_timestamp, col_name].values[0]
        max_value = self.ber_tele_data.loc[self.ber_tele_data[Constants.TIMESTAMP] == max_timestamp, col_name].values[0]
        actual_speed = self.speed_types.get(port_speed, 100000)
        return (max_value - min_value) / ((max_timestamp - min_timestamp) * actual_speed * port_width)
    
    def calc_ber_rates(self, port_speed, port_width, min_timestamp, max_timestamp):
        raw_rate = self.calc_single_rate(min_timestamp, max_timestamp, Constants.RAW_BER)
        eff_rate = self.calc_single_rate(min_timestamp, max_timestamp, Constants.EFF_BER)
        symbol_rate = self.calc_single_rate(min_timestamp, max_timestamp, Constants.SYMBOL_BER)
        return raw_rate, eff_rate, symbol_rate
        
        

    def check_ber_threshold(self, port_name, port_speed, asic, fec_mode, raw_ber_val, eff_ber_val, symbol_ber_val, isolation=True):
        if not asic:
            logger.debug(f"{port_name} doesn't support asic retrieval. skipping BER check")
            return False
        if isolation:
            raw_ber_string = Constants.CSV_RAW_BER_ISOLATE
            eff_ber_string = Constants.CSV_EFF_BER_ISOLATE
            symbol_ber_string = Constants.CSV_SYMBOL_BER_ISOLATE
        else:
            raw_ber_string = Constants.CSV_RAW_BER_DEISOLATE
            eff_ber_string = Constants.CSV_EFF_BER_DEISOLATE
            symbol_ber_string = Constants.CSV_SYMBOL_BER_DEISOLATE

        rows = self.isolation_matrix[(self.isolation_matrix[Constants.ACTIVE_SPEED] == port_speed) &
         (self.isolation_matrix[Constants.CSV_FEC_OPCODE] == fec_mode) &
         (self.isolation_matrix[Constants.CSV_ASIC] == asic) &
         ((raw_ber_val > self.isolation_matrix[raw_ber_string]) |
          (eff_ber_val > self.isolation_matrix[eff_ber_string]) |
          (symbol_ber_val > self.isolation_matrix[symbol_ber_string]))]
        # Check if any rows were returned
        if rows.empty:
            return False
        else:
            return True

    def get_ports_metadata(self):
        meta_data = self.ufm_client.get_ports_metadata()
        if meta_data and len(meta_data) > 0:
            for port in meta_data:
                port_name = port.get(Constants.PORT_NAME)
                if not self.ports_data.get(port_name):
                    self.ports_data[port_name] = {}
                self.ports_data[port_name][Constants.ACTIVE_SPEED] = port.get(Constants.ACTIVE_SPEED)
                self.ports_data[port_name][Constants.ASIC] = port.get(Constants.HW_TECHNOLOGY)
                port_width = port_data.get(Constants.WIDTH)
                port_width = int(port_width.strip('x'))    
                self.ports_data[port_name][Constants.WIDTH] = port_width

    def get_port_metadata(self, port_name):
        meta_data = self.ufm_client.get_port_metadata(port_name)
        if meta_data and len(meta_data) > 0:
            port_data = meta_data[0]
            port_speed = port_data.get(Constants.ACTIVE_SPEED)
            port_asic = port_data.get(Constants.HW_TECHNOLOGY)
            port_width = port_data.get(Constants.WIDTH)
            port_width = int(port_width.strip('x'))
            return port_speed, port_asic, port_width


    def set_ports_as_treated(self, ports_dict):
        for port, state in ports_dict.items():
            port_state = self.ports_states.get(port)
            if port_state and state == Constants.STATE_TREATED:
                port_state.state = state
    
    def get_isolation_state(self):
        ports = self.ufm_client.get_isolated_ports()
        if not ports:
            self.ufm_latest_isolation_state = []
        isolated_ports = [port.split('x')[-1] for port in ports.get(Constants.API_ISOLATED_PORTS, [])]
        self.ufm_latest_isolation_state = isolated_ports
        for port in isolated_ports:
            if not self.ports_states.get(port):
                port_state = PortState(port)
                port_state.update(Constants.STATE_ISOLATED, Constants.ISSUE_OONOC)
                self.ports_states[port] = port_state

    def main_flow(self):
        # sync to the telemetry clock by blocking read
        self.logger.info("Isolation Manager initialized, starting isolation loop")
        self.get_ports_metadata()
        while(True):
            try:
                self.get_isolation_state()
                self.logger.info("Retrieving telemetry data to determine ports' states")
                issues = self.read_next_set_of_high_ber_or_pdr_ports()
                if len(issues) > self.max_num_isolate:
                    # UFM send external event
                    event_msg = "got too many ports detected as unhealthy: %d, skipping isolation" % len(issues)
                    self.logger.warning(event_msg)
                    self.ufm_client.send_event(event_msg)
                
                # deal with reported new issues
                else:
                    for issue in issues.values():
                        port = issue.port
                        cause = issue.cause # ber|pdr|{ber&pdr}

                        self.eval_isolation(port, cause)

                # deal with ports that with either cause = oonoc or fix

                if self.do_deisolate:
                    for port_state in list(self.ports_states.values()):
                        state = port_state.get_state()
                        cause = port_state.get_cause()
                        # EZ: it is a state that say that some maintenance was done to the link 
                        #     so need to re-evaluate if to return it to service
                        if self.automatic_deisolate or cause == Constants.ISSUE_OONOC or state == Constants.STATE_TREATED:
                            self.eval_deisolate(port_state.name)
            except Exception as e:
                self.logger.warning(e)
            time.sleep(self.t_isolate)
            # DEBUG
            #time.sleep(15)
        

# this is a callback for API exposed by this code - second phase
# def work_reportingd(port):
#     PORTS_STATE[port].update(Constants.STATE_TREATED, Constants.ISSUE_INIT)
