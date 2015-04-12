import time
import subprocess
import re

import gobject
import dbus
import dbus.mainloop.glib
from gattlib import GATTRequester

import bluezutils
from UUID import *

# This module is designed to be used like a singleton class
# It wraps the functions of bluez and gattlib and gatttool
# Bluez can't yet do GATT over dbus yet
# gattlib can't do device discovery without root
# neither does service/characteristic discovery, so use gatttool for that
# this is ridiculous

#global definitions
uuid_service = [0x28, 0x00]  # 0x2800
BLUEZ_SERVICE_NAME = 'org.bluez'
BLUEZ_DEVICE_NAME  = 'org.bluez.Device1'
DBUS_OM_IFACE      = 'org.freedesktop.DBus.ObjectManager'
DBUS_PROP_IFACE    = 'org.freedesktop.DBus.Properties'

#UTILITY CLASSES
class Characteristic(object):
    def __init__(self, parent, handle, uuid):
        """
        :param parent: a Peripheral instance
        :param args: args returned by ble_evt_attclient_find_information_found
        :return:
        """
        self.p = parent
        self.handle = handle
        self.uuid   = uuid
        self.byte_value = []
        self.notify_cb = None
    def __hash__(self):
        return self.handle
    def __str__(self):
        return str(self.handle)+":\t"+str(self.uuid)
    def pack(self):
        """
        Subclasses should override this to serialize any instance members
        that need to go in to self.byte_value
        :return:
        """
        pass
    def unpack(self):
        """
        Subclasses should override this to unserialize any instance members
        from self.byte_value
        :return:
        """
        pass
    def write(self):
        self.pack()
        self.p.writeByHandle(self.handle,self.byte_value)
    def read(self):
        self.byte_value = self.p.readByHandle(self.handle)
        self.unpack()
    def onNotify(self, new_value):
        self.byte_value = new_value
        self.unpack()
        if self.notify_cb:
            self.notify_cb()
    def enableNotify(self, enable, cb):
        self.p.enableNotify(self.uuid, enable)
        self.notify_cb = cb

class Peripheral(object):
    def __init__(self, args):
        """
        This is meant to be initialized from a org.bluez.Device1 property list
        :param args: args passed to ble_evt_gap_scan_response
        :return:
        """
        self.sender = str(args['Address'])
        if 'RSSI' in args:
            self.rssi = args['RSSI']
        else:
            self.rssi = -200
        self.conn_handle = None
        self.chars = {} #(handle,Characteristic)
        self.ad_services = [UUID(str(s)) for s in args['UUIDs']]

    def __eq__(self, other):
        return self.sender == other.sender
    def __str__(self):
        s = self.sender
        s+= "\t%d"%self.rssi
        for service in self.ad_services:
            s+="\t"
            s+=str(service)
        return s
    def __repr__(self):
        return self.__str__()

    def connect(self):
        if not self.conn_handle:
            self.conn_handle = GATTRequester(self.sender)
    def disconnect(self):
        pass
    def discover(self):
        groups = discoverServiceGroups(address=self.sender)
        print("Service Groups:")
        for group in groups:
            print(UUID(group['uuid']))
        for group in groups:
            new_group = discoverCharacteristics(address=self.sender,handle_start=group['start'],handle_end=group['end'])
            for c in new_group:
                # FIXME: For some reason the UUIDs are backwards
                #c['uuid'].reverse()
                new_c = Characteristic(self,c['chrhandle'],UUID(c['uuid']))
                self.chars[new_c.handle] = new_c
                print(new_c)
    def findHandleForUUID(self,uuid):
        rval = []
        for c in self.chars.values():
            if c.uuid == uuid:
                rval.append(c.handle)
        if len(rval) != 1:
            raise
        return rval[0]
    def readByHandle(self,char_handle):
        return read(self.conn_handle,char_handle)
    def writeByHandle(self,char_handle,payload):
        return write(self.conn_handle,char_handle,payload)
    def read(self,uuid):
        return self.readByHandle(self.findHandleForUUID(uuid))
    def write(self,uuid,payload):
        return self.writeByHandle(self.findHandleForUUID(uuid),payload)
    def enableNotify(self,uuid,enable):
        # We need to find the characteristic configuration for the provided UUID
        notify_uuid = UUID(0x2902)
        base_handle = self.findHandleForUUID(uuid)
        test_handle = base_handle + 1
        while True:
            if test_handle-base_handle > 3:
                # FIXME: I'm not sure what the error criteria should be, but if we are trying to enable
                # notifications for a characteristic that won't accept it we need to throw an error
                raise
            if self.chars[test_handle].uuid == notify_uuid:
                break
            test_handle += 1
        #test_handle now points at the characteristic config
        if(enable):
            payload = (1,0)
        else:
            payload = (0,0)
        return self.writeByHandle(test_handle,payload)
    def replaceCharacteristic(self,new_char):
        """
        Provides a means to register subclasses of Characteristic with the Peripheral
        :param new_char: Instance of Characteristic or subclass with UUID set.  Handle does not need to be set
        :return:
        """
        handles_by_uuid = dict((c.uuid,c.handle) for c in self.chars.values())
        new_char.handle = handles_by_uuid[new_char.uuid]
        self.chars[new_char.handle] = new_char


#Public facing API

def initialize():
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    gobject.threads_init()

def idle():
    pass

def startScan():
    adapter = bluezutils.find_adapter()
    adapter.StartDiscovery()

def stopScan():
    adapter = bluezutils.find_adapter()
    adapter.StopDiscovery()

def scan(duration,stop_after=0):
    results = {} 
    mainloop = gobject.MainLoop()

    def add_result(path, properties):
        if path in results:
            results[path] = dict(results[path].items() + properties.items())
        else:
            results[path] = properties
        if stop_after>0 and len(results)>=stop_after:
            mainloop.quit()

    def interface_added(path, interfaces):
        if BLUEZ_DEVICE_NAME not in interfaces:
            return
        properties = interfaces[BLUEZ_DEVICE_NAME]
        add_result(path, properties)

    def property_changed(interface, properties, invalidated, path):
        if interface != BLUEZ_DEVICE_NAME:
            return
        add_result(path, properties)

    def _timeout_cb():
        mainloop.quit()
    
    bus=dbus.SystemBus()
    # get existing device entries
    manager = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, "/"), DBUS_OM_IFACE)
    objects = manager.GetManagedObjects()
    for path, interfaces in objects.iteritems():
        interface_added(path, interfaces)

    gobject.timeout_add(duration*1000, _timeout_cb)
    bus.add_signal_receiver(interface_added, dbus_interface = "org.freedesktop.DBus.ObjectManager", signal_name = "InterfacesAdded")
    bus.add_signal_receiver(property_changed, dbus_interface = "org.freedesktop.DBus.Properties", signal_name = "PropertiesChanged", arg0 = BLUEZ_DEVICE_NAME, path_keyword='path')
    
    startScan()
    mainloop.run()
    stopScan()

    return [Peripheral(device) for device in results.values()]

def discoverServiceGroups(address = None, conn = None):
    groups = []
    if address:
        p = subprocess.Popen(['gatttool','-b',address,'--primary'], stdout=subprocess.PIPE)
        for line in p.stdout.readlines():
            match = re.search(r'attr handle.*?(0x[0-9a-fA-F]+).*?end grp handle.*?(0x[0-9a-fA-F]+).*?uuid.*?([-0-9a-fA-F]+)', line)
            if match:
                groups.append({'start': int(match.group(1),0), 'end': int(match.group(2),0), 'uuid': match.group(3) })
    else:
        print("discoverServiceGroups doesn't support connection handles yet")
    return groups

def discoverCharacteristics(address = None, conn = None, handle_start = 0, handle_end = 0xFFFF):
    chars = []
    if address:
        p = subprocess.Popen(['gatttool','-b',address,'--characteristics','-s',str(handle_start),'-e',str(handle_end)], stdout=subprocess.PIPE)
        for line in p.stdout.readlines():
            match = re.search(r'handle.*?(0x[0-9a-fA-F]+).*?char properties.*?(0x[0-9a-fA-F]+).*?char value handle.*?(0x[0-9a-fA-F]+).*?uuid.*?([-0-9a-fA-F]+)', line)
            if match:
                chars.append({'handle': int(match.group(1),0), 'prop': int(match.group(2),0), 'chrhandle': int(match.group(3),0), 'uuid': match.group(4) })
    else:
        print("discoverCharacteristics doesn't support connection handles yet")
    return chars

def read(conn, handle):
    return conn.read_by_handle(handle)

def write(conn, handle, value):
    return conn.write_by_handle(handle,value)

'''
class __waitCB(object):
    def __init__(self,i,r):
        self.i=i
        self.r=r
    def cb(self,ble_instance,args):
        self.r[self.i]=args

def __waitFor(*args):
    """
    Runs a check_activity loop until the rsps and events provided in *args all come in.
    :param args:
    :return:
    """
    retval = [None for a in args]
    cbs = [__waitCB(i,retval) for i in range(len(args))]
    for i in range(len(args)):
        args[i] += cbs[i].cb
    while None in retval:
        idle()
    for i in range(len(args)):
        args[i] -= cbs[i].cb
    return retval
'''

if __name__ == '__main__':
    initialize()
    scan_results = scan(3)
    if len(scan_results) == 0:
        print("No devices found")
        exit(0)


    closest = scan_results[0]
    for s in scan_results:
        if s.rssi > closest.rssi:
            closest = s

    print(closest)
'''
    closest.discover()
    closest.connect()
'''
