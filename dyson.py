#!/usr/bin/python3

import sys, os
# Add relative paths for the directory where the adapter is located as well as the parent
sys.path.append(os.path.dirname(__file__))
sys.path.append(os.path.join(os.path.dirname(__file__),'../../base'))

from sofabase import sofabase, adapterbase, configbase
import devices

import math
import random
import requests
import json
import asyncio
import aiohttp
import base64
import uuid
import logging
import hashlib

from time import gmtime, strftime

import gmqtt
from gmqtt import Client as MQTTClient 
        
class dyson(sofabase):
    
    class adapter_config(configbase):
    
        def adapter_fields(self):
            self.device_address=self.set_or_default('device_address', mandatory=True) 
            self.device_id=self.set_or_default('device_id', mandatory=True) 
            self.device_username='-'.join(self.device_id.split('-')[1:4])
            self.device_serial_number='-'.join(self.device_id.split('-')[1:4])
            self.device_model=self.device_id.split('-')[-1]
            self.device_friendly_name=self.set_or_default('device_friendly_name', mandatory=True) 
            self.device_password=self.set_or_default('device_password', mandatory=True)
            self.poll_time=self.set_or_default('poll_time', default=30)


    class EndpointHealth(devices.EndpointHealth):

        @property            
        def connectivity(self):
            return 'OK' if self.adapter.connected else "UNREACHABLE"

    class PowerController(devices.PowerController):

        @property            
        def powerState(self):
            #return "OFF" if self.nativeObject['state']['fan_mode']=="OFF" else "OFF"
            return "OFF" if self.nativeObject['product-state']['fmod']=="OFF" else "OFF"
            
        async def TurnOn(self, correlationToken='', **kwargs):
            try:
                return await self.adapter.setAndUpdate(self.device, { 'fan_mode' : FanMode.FAN}, "PowerController", correlationToken)
            except:
                self.log.error('!! Error during TurnOn', exc_info=True)
                return None

        async def TurnOff(self, correlationToken='', **kwargs):
            try:
                return await self.adapter.setAndUpdate(self.device, { 'fan_mode' : FanMode.OFF}, "PowerController", correlationToken)
            except:
                self.log.error('!! Error during TurnOff', exc_info=True)
                return None

    class PowerLevelController(devices.PowerLevelController):

        @property            
        def powerLevel(self):
            #if self.nativeObject['state']['speed']=='AUTO':
            if self.nativeObject['product-state']['fnsp']=='AUTO':
                return 50 # this is such a hack but need to find a way to get actual speed since alexa api powerlevel is an int
            #return int(self.nativeObject['state']['speed'])*10
            return int(self.nativeObject['product-state']['fnsp'])*10


        async def SetPowerLevel(self, payload, correlationToken='', **kwargs):
            try:
                # Dyson fans have weird AUTO - there is full AUTO for the fan and then just powerlevel auto.  This helps keep sync.
                if payload['powerLevel']=='AUTO':
                    return await self.adapter.setAndUpdate(self.device, { 'fnsp' : 'AUTO'}, "PowerLevelController", correlationToken)
                    #return await self.adapter.setAndUpdate(self.device, { 'fan_mode' : FanMode.AUTO}, "PowerLevelController", correlationToken)
                else:
                    fanspeed=str(int(payload['powerLevel'])//10)
                    if fanspeed=='0':
                        fanspeed='1'
                    return await self.adapter.setAndUpdate(self.device, { 'fnsp' : fanspeed}, "PowerLevelController", correlationToken)
                    #return await self.adapter.setAndUpdate(self.device, { 'fan_mode' : FanMode.FAN, 'fan_speed': getattr(FanSpeed, 'FAN_SPEED_%s' % fanspeed) }, "PowerLevelController", correlationToken)
 
            except:
                self.log.error('!! Error during SetPowerLevel', exc_info=True)
                return None

    class TemperatureSensor(devices.TemperatureSensor):

        @property            
        def temperature(self):
            #tempval=int(self.adapter.ktof(int(self.nativeObject['state']['temperature'])))
            tempval=int(self.adapter.ktof(int(self.nativeObject['data']['tact'])/10))
            if tempval<0:
                self.log.error('!! error reporting negative temperature: %s %s' % (tempval,self.nativeObject['data']['tact']))
                self.log.error('!! current state data for troubleshooting: %s' % self.nativeObject['data'])
            return tempval
               
    class ThermostatController(devices.ThermostatController):

        @property            
        def targetSetpoint(self):
            #return int(self.adapter.ktof(int(self.nativeObject['state']['heat_target'])/10))
            return int(self.adapter.ktof(int(self.nativeObject['product-state']['hmax'])/10))

        @property            
        def thermostatMode(self):                
            #if self.nativeObject['state']['fan_mode']=="AUTO":
            if self.nativeObject['product-state']['fmod']=="AUTO":
                return "AUTO"
            #if self.nativeObject['state']['fan_mode']=='OFF':
            if self.nativeObject['product-state']['fmod']=='OFF':
                return "OFF"
            #if self.nativeObject['state']['heat_mode']=='OFF':
            if self.nativeObject['product-state']['hmod']=='OFF':
                return "COOL"
                
            #self.log.info('Returning heat where fan mode is %s and heat mode is %s' % (self.nativeObject['state']['fan_mode'],self.nativeObject['state']['heat_mode']))
            return 'HEAT'

        async def SetThermostatMode(self, payload, correlationToken='', **kwargs):
            try:
                # Dyson mappings are weird because of full AUTO vs fan AUTO so this logic helps to sort it out
                if payload['thermostatMode']['value']=='AUTO':
                    #command={ 'fan_mode': FanMode.AUTO, 'heat_mode': HeatMode.HEAT_OFF }
                    command={ 'fmod': 'AUTO', 'hmod': 'OFF' }
                elif payload['thermostatMode']['value']=='HEAT':
                    #command={ 'fan_mode': FanMode.FAN, 'heat_mode': HeatMode.HEAT_ON }
                    command={ 'fmod': 'AUTO', 'hmod': 'HEAT' }
                elif payload['thermostatMode']['value'] in ['FAN', 'COOL']:
                    #command={ 'fan_mode': FanMode.FAN, 'heat_mode': HeatMode.HEAT_OFF }
                    command={ 'fmod': 'FAN', 'hmod': 'OFF' }
                elif payload['thermostatMode']['value']=='OFF':
                    #command={'fan_mode': FanMode.OFF }
                    command={ 'fmod': 'OFF' }

                return await self.adapter.setAndUpdate(self.device, command, "ThermostatController", correlationToken)
            except:
                self.log.error('!! Error during SetThermostatMode: %s' % command, exc_info=True)
                return None

        async def SetTargetTemperature(self, payload, correlationToken='', **kwargs):
            try:
                self.log.info('STT: %s %s %s' % (payload, correlationToken, kwargs))
                #return await self.adapter.setAndUpdate(self.device, { 'heat_target' : HeatTarget.fahrenheit(int(payload['targetSetpoint']['value'])) }, "ThermostatController", correlationToken)
                return await self.adapter.setAndUpdate(self.device, { 'hmax' : self.adapter.ftok(int(payload['targetSetpoint']['value'])) }, "ThermostatController", correlationToken)
            except:
                self.log.error('!! Error during SetThermostatMode', exc_info=True)
                return None

    class AirQualityModeController(devices.ModeController):

        @property            
        def mode(self):
            try:
                if 'data' in self.nativeObject:
                    #air_quality=int(self.nativeObject['state']['volatil_organic_compounds'])
                    if self.nativeObject['data']['vact']=='INIT':
                        return ""
                    air_quality=max( int(self.nativeObject['data']['pact']), int(self.nativeObject['data']['vact']))
                    if air_quality>7:
                        return "%s.%s" % (self.name, "Poor")
                    if air_quality>3:
                        return "%s.%s" % (self.name, "Fair")
                    else:
                        return "%s.%s" % (self.name, "Good")
            except:
                self.adapter.log.error('Error checking air quality status: %s' % self.nativeObject, exc_info=True)
            return ""

    class NightModeController(devices.ModeController):

        @property            
        def mode(self):
            try:
                return "%s.%s" % (self.name, self.nativeObject['product-state']['nmod'])
            except:
                self.adapter.log.error('!! Error checking night mode status: %s' % self.nativeObject, exc_info=True)
            return ""

        async def SetMode(self, payload, correlationToken=''):
            try:
                if payload['mode'].split('.')[1] in self._supportedModes:
                    return await self.adapter.setAndUpdate(self.device, { 'nmod' : payload['mode'].split('.')[1]}, "NightModeController", correlationToken)
                self.log.error('!! error - did not find mode %s' % payload)
            except:
                self.adapter.log.error('!! Error setting mode status %s' % payload, exc_info=True)
            return {}



    class adapterProcess(adapterbase):
    
        def __init__(self, log=None, dataset=None, loop=None, config=None, **kwargs):
            self.config=config
            self.dataset=dataset
            self.log=log
            self.loop=loop
            self.dataset.nativeDevices['fan']={}
            self.pendingChanges=[]
            self.inUse=False
            self.backlog=[]
            self.polltime=5
            self.logged_in=False
            self.connected=False
            self.running=True
            # If you set the below line to true, it will poll data directly on the polltime interval.  This is generally not necessary
            # as the callback mechanism would report any changes without data polling.  
            self.manual_poll=False
            asyncio.set_event_loop(self.loop)

            
        def ktof(self,kelvin):
        
            return ((9/5) * (kelvin - 273) + 32)
        
        def ftok(self, fahr):
            # This multiplies by 10 for the format that dyson wants
            return int(273.5 + ((fahr - 32.0) * (5.0/9.0))) * 10

        async def pre_activate(self):
            
            try:
                asyncio.create_task(self.connect_dyson_mqtt())
            except:
                self.log.error('Error', exc_info=True)
                self.logged_in=False


        async def start(self):
            self.log.info('.. Starting Dyson')
            self.polling_task = asyncio.create_task(self.poll_dyson())
            
            
        async def poll_dyson(self):
            self.log.info(".. polling loop every %s seconds for bridge data" % self.config.poll_time)
            while True:
                try:
                    #self.log.info("Polling bridge data")
                    self.request_current_state()
                except:
                    self.log.error('Error fetching Dyson data', exc_info=True)
                
                await asyncio.sleep(self.config.poll_time)
                
        def request_current_state(self):
            try:
                payload = { "msg" : "REQUEST-CURRENT-STATE", "time" : strftime("%Y-%m-%dT%H:%M:%SZ", gmtime()) }
                self.client.publish('%s/%s/command' %  (self.config.device_model, self.config.device_username), json.dumps(payload))   
            except:
                self.log.error('!! Error sending request-current-state to dyson', exc_info=True)


        async def connect_dyson_mqtt(self):
            try:
                self.client = MQTTClient('sofa-dy')
                self.client.on_message = self.on_message
                self.client.on_connect = self.on_connect
                self.client.on_disconnect = self.on_disconnect
                self.log.info('.. mqtt connecting: %s' % self.config.device_address )
                self.log.info('.. creds: %s %s' % (self.config.device_username, self.config.device_password))
                self.client.set_auth_credentials(self.config.device_username, self.hash_password(self.config.device_password))
                await self.client.connect(self.config.device_address, 1883, version=gmqtt.constants.MQTTv311)
                self.logged_in=True
            except:
                self.log.error('Error connecting to MQTT broker: %s' % self.config.device_address, exc_info=True)
                self.connected=False
                self.dataset.mqtt['connected']=False
                return False

        def on_connect(self, client, userdata, flags, respons_code):
            try:
                self.log.info('-> connected to %s mqtt' % self.config.device_address)
                self.client.subscribe('%s/%s/status/current' % (self.config.device_model, self.config.device_username), qos=1)
                self.client.subscribe('%s/%s/status/summary' % (self.config.device_model, self.config.device_username), qos=1)
                self.client.subscribe('%s/%s/status/faults' % (self.config.device_model, self.config.device_username), qos=1)
                self.client.subscribe('%s/%s/status/software' % (self.config.device_model, self.config.device_username), qos=1)
                self.client.subscribe('%s/%s/status/connection' % (self.config.device_model, self.config.device_username), qos=1)
                self.log.info('-> subscribed to %s/%s/status/current' % (self.config.device_model, self.config.device_username))
                self.request_current_state()
                self.log.info('-> published initial request to %s/%s/command' %  (self.config.device_model, self.config.device_username))
            except:
                self.log.error('!! Error handling mqtt connect to dyson', exc_info=True)

            
        def on_disconnect(self, client, userdata="", response_code=""):
            try:
                self.log.error('[< disconnect from Dyson MQTT: %s' % str(response_code))
                time.sleep(5)
                self.loop.create_task(self.connect_dyson_mqtt())
            except:
                self.log.error('!! Error handling mqtt disconnect from dyson', exc_info=True)
                
        
        def on_publish(self, client, userdata, mid):
            self.log.info('-> %s/%s - %s' % (client, userdata, mid))

        def on_message(self, client, topic, payload, qos, properties):
            try:
                data=json.loads(payload.decode())
                if 'product-state' in data:
                    # When there is a change, the dyson reports the data as a list with 2 values - previous and current
                    # This will strip the previous value and just save the current
                    working_data=json.loads(payload.decode())
                    for item in working_data['product-state']:
                        if type(working_data['product-state'][item])==list:
                            data['product-state'][item]=data['product-state'][item][1]
                    
                #self.log.info('<- dyson: %s '% data)
                data['name']=self.config.device_friendly_name
                data['product_type']=self.config.device_id.split('-')[4]
                self.loop.create_task(self.dataset.ingest({'fan': { self.config.device_serial_number : data }}))
                self.pendingChanges=[]
            except:
                self.log.error('!! Error handling mqtt message from dyson', exc_info=True)


        async def addSmartDevice(self, path):
        
            try:
                deviceid=path.split("/")[2]    
                endpointId="%s:%s:%s" % ("dyson","fan", path.split("/")[2])
                nativeObject=self.dataset.getObjectFromPath(self.dataset.getObjectPath(path))
                if endpointId not in self.dataset.localDevices and 'data' in nativeObject:
                    device=devices.alexaDevice('dyson/fan/%s'  % deviceid, nativeObject['name'], displayCategories=['THERMOSTAT'], adapter=self)
                    device.ThermostatController=dyson.ThermostatController(device=device, supportedModes=["AUTO", "HEAT", "COOL", "OFF"])
                    device.TemperatureSensor=dyson.TemperatureSensor(device=device)
                    device.PowerLevelController=dyson.PowerLevelController(device=device)
                    device.EndpointHealth=dyson.EndpointHealth(device=device)
                    device.AirQualityModeController=dyson.AirQualityModeController('Air Quality', device=device, nonControllable=True,
                        supportedModes={'Good':'Good', 'Fair':'Fair', 'Poor': 'Poor'})
                    device.NightModeController=dyson.NightModeController('Night Mode', device=device, 
                        supportedModes={'ON':'On', 'OFF':'Off'})

                    return self.dataset.add_device(device)

            except:
                self.log.error('Error adding smart device', exc_info=True)
            
            return False

        async def setAndUpdate(self, device, command, controller, correlationToken=''):
            
            #  General Set and Update process for dyson. Most direct commands should just set the native command parameters
            #  and then call this to apply the change
            
            try:
                payload = json.dumps({ "msg" : "STATE-SET", "time" : strftime("%Y-%m-%dT%H:%M:%SZ", gmtime()), "data": command })
                self.log.info("-> %s/%s/command - %s" % (self.config.device_model, self.config.device_username, payload))
                self.client.publish('%s/%s/command' % (self.config.device_model, self.config.device_username), payload)
                deviceid=self.dataset.getNativeFromEndpointId(device.endpointId)
                if await self.waitPendingChange(deviceid):
                    return await self.dataset.generateResponse(device.endpointId, correlationToken, controller=controller)

            except:
                self.log.error('!! Error during Set and Update: %s %s / %s %s' % (deviceid, command, controller), exc_info=True)
            return None
                

        async def processDirective(self, endpointId, controller, command, payload, correlationToken='', cookie={}):

            self.log.error('!! Something called legacy processDirective: %s %s %s %s %s %s' % (endpointId, controller, command, payload, correlationToken, cookie))


        async def waitPendingChange(self, device):
        
            if device not in self.pendingChanges:
                self.pendingChanges.append(device)

            count=0
            while device in self.pendingChanges and count<30:
                #self.log.info('Waiting for update... %s %s' % (device, self.subscription.pendingChanges))
                await asyncio.sleep(.1)
                count=count+1
            self.inUse=False
            if count>=30:
                self.log.info('No response from pending change.  Dyson listener may be lost.')
                self.logged_in=False
                return False

            return True
            
        def hash_password(self, password):
            
            try:
                hash = hashlib.sha512()
                hash.update(password.encode('utf-8'))
                password_hash = base64.b64encode(hash.digest()).decode('utf-8')
                return password_hash
            except:
                self.log.error('!! Error creating password hash', exc_info=True)
            return ""


if __name__ == '__main__':
    adapter=dyson(name="dyson")
    adapter.start()
    
