#!/usr/bin/python3

import sys, os
# Add relative paths for the directory where the adapter is located as well as the parent
sys.path.append(os.path.dirname(__file__))
sys.path.append(os.path.join(os.path.dirname(__file__),'../../base'))

from sofabase import sofabase
from sofabase import adapterbase
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

from libpurecool.dyson import DysonAccount
from libpurecool.const import FanSpeed, FanMode, NightMode, Oscillation, FanState, StandbyMonitoring, QualityTarget, ResetFilter, HeatMode, FocusMode, HeatTarget
from libpurecool.dyson_pure_state import DysonPureHotCoolState, DysonPureCoolState, DysonEnvironmentalSensorState
        
class dyson(sofabase):

    class EndpointHealth(devices.EndpointHealth):

        @property            
        def connectivity(self):
            return 'OK' if self.adapter.connected else "UNREACHABLE"

    class PowerController(devices.PowerController):

        @property            
        def powerState(self):
            return "OFF" if self.nativeObject['state']['fan_mode']=="OFF" else "OFF"
        
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
            if self.nativeObject['state']['speed']=='AUTO':
                return 50 # this is such a hack but need to find a way to get actual speed since alexa api powerlevel is an int
            return int(self.nativeObject['state']['speed'])*10


        async def SetPowerLevel(self, payload, correlationToken='', **kwargs):
            try:
                # Dyson fans have weird AUTO - there is full AUTO for the fan and then just powerlevel auto.  This helps keep sync.
                if payload['powerLevel']=='AUTO':
                    return await self.adapter.setAndUpdate(self.device, { 'fan_mode' : FanMode.AUTO}, "PowerLevelController", correlationToken)
                else:
                    fanspeed=str(int(payload['powerLevel'])//10)
                    if fanspeed=='0':
                        fanspeed='1'
                    return await self.adapter.setAndUpdate(self.device, { 'fan_mode' : FanMode.FAN, 'fan_speed': getattr(FanSpeed, 'FAN_SPEED_%s' % fanspeed) }, "PowerLevelController", correlationToken)
 
            except:
                self.log.error('!! Error during SetPowerLevel', exc_info=True)
                return None

    class TemperatureSensor(devices.TemperatureSensor):

        @property            
        def temperature(self):
            return int(self.adapter.ktof(int(self.nativeObject['state']['temperature'])))
               
    class ThermostatController(devices.ThermostatController):

        @property            
        def targetSetpoint(self):                
            return int(self.adapter.ktof(int(self.nativeObject['state']['heat_target'])/10))

        @property            
        def thermostatMode(self):                
            if self.nativeObject['state']['fan_mode']=="AUTO":
                return "AUTO"
            if self.nativeObject['state']['fan_mode']=='OFF':
                return "OFF"
            if self.nativeObject['state']['heat_mode']=='OFF':
                return "COOL"
                
            #self.log.info('Returning heat where fan mode is %s and heat mode is %s' % (self.nativeObject['state']['fan_mode'],self.nativeObject['state']['heat_mode']))
            return 'HEAT'

        async def SetThermostatMode(self, payload, correlationToken='', **kwargs):
            try:
                # Dyson mappings are weird because of full AUTO vs fan AUTO so this logic helps to sort it out
                if payload['thermostatMode']['value']=='AUTO':
                    command={ 'fan_mode': FanMode.AUTO, 'heat_mode': HeatMode.HEAT_OFF }
                elif payload['thermostatMode']['value']=='HEAT':
                    command={ 'fan_mode': FanMode.FAN, 'heat_mode': HeatMode.HEAT_ON }
                elif payload['thermostatMode']['value'] in ['FAN', 'COOL']:
                    command={ 'fan_mode': FanMode.FAN, 'heat_mode': HeatMode.HEAT_OFF }
                elif payload['thermostatMode']['value']=='OFF':
                    command={'fan_mode': FanMode.OFF }

                return await self.adapter.setAndUpdate(self.device, command, "ThermostatController", correlationToken)
            except:
                self.log.error('!! Error during SetThermostatMode', exc_info=True)
                return None

        async def SetTargetTemperature(self, payload, correlationToken='', **kwargs):
            try:
                return await self.adapter.setAndUpdate(self.device, { 'heat_target' : HeatTarget.fahrenheit(int(payload['targetSetpoint']['value'])) }, "ThermostatController", correlationToken)
            except:
                self.log.error('!! Error during SetThermostatMode', exc_info=True)
                return None



    class adapterProcess(adapterbase):
    
        def __init__(self, log=None, dataset=None, notify=None, request=None, loop=None, **kwargs):
            self.dataset=dataset
            self.log=log
            self.notify=notify
            self.account = DysonAccount(self.dataset.config['user'],self.dataset.config['password'],self.dataset.config['region'])
            self.dataset.nativeDevices['fan']={}
            self.pendingChanges=[]
            self.inUse=False
            self.backlog=[]
            self.polltime=5
            self.logged_in=False
            self.connected=False
            self.running=True
            #self.log.info('Loggers: %s' % logging.root.manager.loggerDict)
            logging.getLogger('libpurecoollink').setLevel(logging.DEBUG)
            logging.getLogger('libpurecoollink.dyson_device').setLevel(logging.DEBUG)
            if not loop:
                self.loop = asyncio.new_event_loop()
            else:
                self.loop=loop
            asyncio.set_event_loop(self.loop)
                
                
        async def connect_dyson(self):
        
            return self.account.login()
            
        def ktof(self,kelvin):
        
            return ((9/5) * (kelvin - 273) + 32)
            
        def on_message(self, msg):
            
            try:
                self.pendingChanges=[]
                #self.log.info('-> %s' % msg)
                newstate={}
                allprops=['humidity','temperature','dust','sleep_timer',"fan_mode", "fan_state", "night_mode", "speed", "oscillation", "filter_life", "quality_target", "standby_monitoring", "tilt", "focus_mode", "heat_mode", "heat_target", "heat_state"]
                props=filter(lambda a: not a.startswith('_'), dir(msg))
                for prop in props:
                    if prop in allprops:
                        newstate[prop]=getattr(msg,prop)
                        if prop=='temperature':
                            # Since Alexa API thermostats can only handle full degrees, this rounds the temp off 
                            # to prevent mid-degree temperature updates and prevents general spam on the bus
                            newstate[prop]=int(newstate[prop])
                #self.log.info('New State: %s' % newstate)
                asyncio.ensure_future(self.dataset.ingest({'fan': {self.dyson_devices[0].name+" fan": { "state": newstate} }}), loop=self.loop)
            except:
                self.log.error('Error handling message: %s' % msg, exc_info=True)

        async def stop(self):
            
            try:
                self.log.info('Stopping Dyson devices')
                for machine in self.dyson_devices:
                    machine.disconnect()
                self.running=False
            except:
                self.log.error('Error stopping Dyson connection', exc_info=True)

        def service_stop(self):
            
            try:
                self.log.info('Stopping Dyson devices')
                for machine in self.dyson_devices:
                    machine.disconnect()
                self.running=False
            except:
                self.log.error('Error stopping Dyson connection', exc_info=True)
            
        async def start(self):
            
            self.log.info('Starting Dyson')
            try:
                await self.keep_logged_in()
            except:
                self.log.error('Error', exc_info=True)
                self.logged_in=False

        async def connect_dyson(self):
            try:
                if not self.logged_in:
                    self.logged_in=self.account.login()
                    if self.logged_in:
                        self.dyson_devices = self.account.devices()
                        self.log.info('Devices: %s' % self.dyson_devices)
                        for device in self.dyson_devices:
                            devconfig={}
                            settings=["serial","active","name","version","auto_update","new_version_available","product_type","network_device"]
                            for setting in settings:
                                if setting=="name":
                                    devconfig[setting]=getattr(device,setting)+" fan"
                                else:
                                    devconfig[setting]=getattr(device,setting)
                            if 'device_address' in self.dataset.config:
                                self.connected = device.connect(self.dataset.config["device_address"]) 
                            else:
                                self.connected = device.auto_connect() # uses mdns to find the device

                            if self.connected:
                                self.dyson_devices[0].add_message_listener(self.on_message)
                                devconfig['state']=await self.getFanProperties(self.dyson_devices[0])
                                await self.dataset.ingest({'fan': {device.name+" fan": devconfig}})
                    else:
                        self.log.warn('!! Warning - not logged in after connect: %s' % self.account._password)
            except:
                self.log.error('Error', exc_info=True)

        async def keep_logged_in(self):
            
            while self.running:
                try:
                    if not self.logged_in:
                        await self.connect_dyson()
                    await asyncio.sleep(self.polltime)
                except:
                    self.log.error('Error fetching Dyson Data', exc_info=True)
                    self.logged_in=False

        async def setDyson(self, device, command):
            #devices[0].set_configuration(heat_mode=HeatMode.HEAT_ON, heat_target=HeatTarget.fahrenheit(80), fan_speed=FanSpeed.FAN_SPEED_5)
            #self.dyson_devices[0].set_configuration(heat_mode=HeatMode.HEAT_OFF, fan_mode=FanMode.FAN, fan_speed=FanSpeed.FAN_SPEED_2)
            
            try:
                qid=uuid.uuid1()
                if self.inUse:
                    myturn=False
                    self.backlog.append(qid)
                    self.log.info('Holding change %s due to interface in use: %s' % (qid, command))
                    try:
                        while not myturn:
                            if not self.inUse and self.backlog[0]==qid:
                                myturn=True
                                self.backlog.remove(qid)
                            else:
                                await asyncio.sleep(.1)
                    except:
                        self.log.info('Something failed with backlog checking', exc_info=True)
                self.inUse=True
                self.log.info('Setting configuration %s : %s' %  (qid, command))
                self.dyson_devices[0].set_configuration(**command)
            except:
                self.log.error('Error setting config', exc_info=True)

        def percentage(self, percent, whole):
            return int((percent * whole) / 100.0)


        async def addSmartDevice(self, path):
        
            try:
                deviceid=path.split("/")[2]    
                nativeObject=self.dataset.getObjectFromPath(self.dataset.getObjectPath(path))
                if nativeObject['name'] not in self.dataset.localDevices:
                    if nativeObject["product_type"]=="455":
                        device=devices.alexaDevice('dyson/fan/%s'  % deviceid, nativeObject['name'], displayCategories=['THERMOSTAT'], adapter=self)
                        device.ThermostatController=dyson.ThermostatController(device=device, supportedModes=["AUTO", "HEAT", "COOL", "OFF"])
                        device.TemperatureSensor=dyson.TemperatureSensor(device=device)
                        device.PowerLevelController=dyson.PowerLevelController(device=device)
                        device.EndpointHealth=dyson.EndpointHealth(device=device)
                        return self.dataset.newaddDevice(device)

            except:
                self.log.error('Error adding smart device', exc_info=True)
            
            return False
            
        async def getFanProperties(self, device):
        
            try:
                dev=self.dyson_devices[0]
                devstate={}
                rawstate=self.dyson_devices[0].state
                envstate=self.dyson_devices[0]._environmental_state

                settings=["fan_mode", "fan_state", "night_mode", "speed", "oscillation", "filter_life", "quality_target", "standby_monitoring", "tilt", "focus_mode", "heat_mode", "heat_target", "heat_state"]
                for setting in settings:
                    devstate[setting]=getattr(self.dyson_devices[0].state,setting)
                                
                settings=['humidity','temperature','dust','sleep_timer']
                for setting in settings:                            
                    devstate[setting]=getattr(envstate,setting)
                    
                return devstate
            except:
                self.log.error('Error getting Fan properties', exc_info=True)
                return {}
                
        async def setAndUpdate(self, device, command, controller, correlationToken=''):
            
            #  General Set and Update process for dyson. Most direct commands should just set the native command parameters
            #  and then call this to apply the change
            
            try:
                self.log.info('.. using new update methods')
                deviceid=self.dataset.getNativeFromEndpointId(device.endpointId)
                await self.setDyson(deviceid, command)
                if await self.waitPendingChange(deviceid):
                    updatedProperties=await self.getFanProperties(deviceid)
                    await self.dataset.ingest({'fan': { deviceid: {'state':updatedProperties}}})
                    return await self.dataset.generateResponse(device.endpointId, correlationToken, controller=controller)
            except:
                self.log.error('!! Error during Set and Update: %s %s / %s %s' % (deviceid, command, controller), exc_info=True)
                return None
                

        async def processDirective(self, endpointId, controller, command, payload, correlationToken='', cookie={}):

            self.log.error('!! Something called legacy processDirective: %s %s %s %s %s %s' % (endpointId, controller, command, payload, correlationToken, cookie))


        async def waitPendingChange(self, device):
        
            if device not in self.pendingChanges:
                self.pendingChanges.append(device)
                self.log.info('Adding device to pending change')

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


if __name__ == '__main__':
    adapter=dyson(name="dyson")
    adapter.start()
    
