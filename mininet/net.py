"""

    Mininet-WiFi: A simple networking testbed for Wireless OpenFlow/SDN!

author: Bob Lantz (rlantz@cs.stanford.edu)
author: Brandon Heller (brandonh@stanford.edu)

Modified by Ramon Fontes (ramonrf@dca.fee.unicamp.br)

Mininet creates scalable OpenFlow test networks by using
process-based virtualization and network namespaces.

Simulated hosts/stations are created as processes in separate network
namespaces. This allows a complete OpenFlow network to be simulated on
top of a single Linux kernel.

Each host has:

A virtual console (pipes to a shell)
A virtual interface (half of a veth pair)
A parent shell (and possibly some child processes) in a namespace

Hosts have a network interface which is configured via ip addr/ip
link/etc.

Each station has:

A virtual console (pipes to a shell)
A virtual wireless interface (half of a veth pair)
A parent shell (and possibly some child processes) in a namespace

This version supports both the kernel and user space datapaths
from the OpenFlow reference implementation (openflowswitch.org)
as well as OpenVSwitch (openvswitch.org.)

In kernel datapath mode, the controller and switches are simply
processes in the root namespace.

Kernel OpenFlow datapaths are instantiated using dpctl(8), and are
attached to the one side of a veth pair; the other side resides in the
host namespace. In this mode, switch processes can simply connect to the
controller via the loopback interface.

In user datapath mode, the controller and switches can be full-service
nodes that live in their own network namespaces and have management
interfaces and IP addresses on a control network (e.g. 192.168.123.1,
currently routed although it could be bridged.)

In addition to a management interface, user mode switches also have
several switch interfaces, halves of veth pairs whose other halves
reside in the host nodes that the switches are connected to.

Consistent, straightforward naming is important in addLinkorder to easily
identify hosts, switches and controllers, both from the CLI and
from program code. Interfaces are named to make it easy to identify
which interfaces belong to which node.

The basic naming scheme is as follows:

    Host nodes are named h1-hN
    Switch nodes are named s1-sN
    Controller nodes are named c0-cN
    Interfaces are named {nodename}-eth0 .. {nodename}-ethN

Note: If the network topology is created using mininet.topo, then
node numbers are unique among hosts and switches (e.g. we have
h1..hN and SN..SN+M) and also correspond to their default IP addresses
of 10.x.y.z/8 where x.y.z is the base-256 representation of N for
hN. This mapping allows easy determination of a node's IP
address from its name, e.g. h1 -> 10.0.0.1, h257 -> 10.0.1.1.

Note also that 10.0.0.1 can often be written as 10.1 for short, e.g.
"ping 10.1" is equivalent to "ping 10.0.0.1".

Currently we wrap the entire network in a 'mininet' object, which
constructs a simulated network based on a network topology created
using a topology object (e.g. LinearTopo) from mininet.topo or
mininet.topolib, and a Controller which the switches will connect
to. Several configuration options are provided for functions such as
automatically setting MAC addresses, populating the ARP table, or
even running a set of terminals to allow direct interaction with nodes.

After the network is created, it can be started using start(), and a
variety of useful tasks maybe performed, including basic connectivity
and bandwidth tests and running the mininet CLI.

Once the network is up and running, test code can easily get access
to host and switch objects which can then be used for arbitrary
experiments, typically involving running a series of commands on the
hosts.

After all desired tests or activities have been completed, the stop()
method may be called to shut down the network.

"""
import os
import re
import select
import signal
import random

from time import sleep
from itertools import chain, groupby
from math import ceil

from mininet.cli import CLI
from mininet.log import info, error, debug, output, warn
from mininet.node import (Node, Host, Station, Car, AP, OVSKernelSwitch,
                          OVSKernelAP, DefaultController, Controller)
from mininet.nodelib import NAT
from mininet.link import Link, Intf, _4addrLink, TCIntfWireless
from mininet.wifiLink import Association
from mininet.util import (quietRun, fixLimits, numCores, ensureRoot,
                          macColonHex, ipStr, ipParse, netParse, ipAdd,
                          waitListening)
from mininet.term import cleanUpScreens, makeTerms
from mininet.wifiNet import mininetWiFi

# Mininet version: should be consistent with README and LICENSE
VERSION = "2.2.0d1"

class Mininet(object):
    "Network emulation with hosts spawned in network namespaces."

    def __init__(self, topo=None, switch=OVSKernelSwitch,
                 accessPoint=OVSKernelAP, host=Host, station=Station,
                 car=Car, controller=DefaultController, isWiFi=False,
                 link=Link, intf=Intf, build=True, xterms=False,
                 cleanup=False, ipBase='10.0.0.0/8', inNamespace=False,
                 autoSetMacs=False, autoStaticArp=False, autoPinCpus=False,
                 listenPort=None, waitConnected=False, ssid="new-ssid",
                 mode="g", channel="1", enable_wmediumd=False,
                 enable_interference=False, enable_spec_prob_link=False,
                 enable_error_prob=False, disableAutoAssociation=False,
                 driver='nl80211', autoSetPositions=False,
                 configureWiFiDirect=False, configure4addr=False,
                 defaultGraph=False, noise_threshold=-91, cca_threshold=-90):
        """Create Mininet object.
           topo: Topo (topology) object or None
           switch: default Switch class
           host: default Host class/constructor
           controller: default Controller class/constructor
           link: default Link class/constructor
           intf: default Intf class/constructor
           ipBase: base IP address for hosts,
           build: build now from topo?
           xterms: if build now, spawn xterms?
           cleanup: if build now, cleanup before creating?
           inNamespace: spawn switches and controller in net namespaces?
           autoSetMacs: set MAC addrs automatically like IP addresses?
           autoStaticArp: set all-pairs static MAC addrs?
           autoPinCpus: pin hosts to (real) cores (requires CPULimitedHost)?
           listenPort: base listening port to open; will be incremented for
               each additional switch in the net if inNamespace=False"""
        self.topo = topo
        self.switch = switch
        self.host = host
        self.station = station
        self.accessPoint = accessPoint
        self.car = car
        self.controller = controller
        self.link = link
        self.intf = intf
        self.ipBase = ipBase
        self.ipBaseNum, self.prefixLen = netParse(self.ipBase)
        self.nextIP = 1  # start for address allocation
        self.nextPosition = 1 # start for position allocation
        self.repetitions = 1 # mobility: number of repetitions
        self.inNamespace = inNamespace
        self.xterms = xterms
        self.cleanup = cleanup
        self.autoSetMacs = autoSetMacs
        self.autoSetPositions = autoSetPositions
        self.autoStaticArp = autoStaticArp
        self.autoPinCpus = autoPinCpus
        self.numCores = numCores()
        self.nextCore = 0  # next core for pinning hosts to CPUs
        self.listenPort = listenPort
        self.waitConn = waitConnected
        self.routing = ''
        self.ssid = ssid
        self.mode = mode
        self.channel = channel
        self.nameToNode = {}  # name to Node (Host/Switch) objects
        self.aps = []
        self.controllers = []
        self.hosts = []
        self.links = []
        self.cars = []
        self.carsSW = []
        self.carsSTA = []
        self.switches = []
        self.stations = []
        self.walls = []
        self.terms = []  # list of spawned xterm processes
        self.driver = driver
        self.disableAutoAssociation = disableAutoAssociation
        self.mobilityKwargs = ''
        self.isMobilityModel = False
        self.isMobility = False
        self.ppm_is_set = False
        self.noise_threshold = noise_threshold
        self.cca_threshold = cca_threshold
        self.configureWiFiDirect = configureWiFiDirect
        self.configure4addr = configure4addr
        self.enable_wmediumd = enable_wmediumd
        self.enable_error_prob = enable_error_prob
        self.enable_interference = enable_interference
        self.enable_spec_prob_link = enable_spec_prob_link
        self.isWiFi = isWiFi
        Mininet.init()  # Initialize Mininet if necessary

        if defaultGraph:
            self.defaultGraph()

        self.built = False
        if topo and build:
            self.build()

    def waitConnected(self, timeout=None, delay=.5):
        """wait for each switch to connect to a controller,
           up to 5 seconds
           timeout: time to wait, or None to wait indefinitely
           delay: seconds to sleep per iteration
           returns: True if all switches are connected"""
        info('*** Waiting for switches to connect\n')
        time = 0
        remaining = list(self.switches)
        while True:
            for switch in tuple(remaining):
                if switch.connected():
                    info('%s ' % switch)
                    remaining.remove(switch)
            if not remaining:
                info('\n')
                return True
            if time > timeout and timeout is not None:
                break
            sleep(delay)
            time += delay
        warn('Timed out after %d seconds\n' % time)
        for switch in remaining:
            if not switch.connected():
                warn('Warning: %s is not connected to a controller\n'
                     % switch.name)
            else:
                remaining.remove(switch)
        return not remaining

    def addHost(self, name, cls=None, **params):
        """Add host.
           name: name of host to add
           cls: custom host class/constructor (optional)
           params: parameters for host
           returns: added host"""
        # Default IP and MAC addresses
        defaults = { 'ip': ipAdd(self.nextIP,
                                 ipBaseNum=self.ipBaseNum,
                                 prefixLen=self.prefixLen) +
                           '/%s' % self.prefixLen }
        if self.autoSetMacs:
            defaults[ 'mac' ] = macColonHex(self.nextIP)
        if self.autoPinCpus:
            defaults[ 'cores' ] = self.nextCore
            self.nextCore = (self.nextCore + 1) % self.numCores
        self.nextIP += 1
        defaults.update(params)
        if not cls:
            cls = self.host
        h = cls(name, **defaults)

        self.hosts.append(h)
        self.nameToNode[ name ] = h
        return h

    def delNode(self, node, nodes=None):
        """Delete node
           node: node to delete
           nodes: optional list to delete from (e.g. self.hosts)"""
        if nodes is None:
            nodes = (self.hosts if node in self.hosts else
                     (self.switches if node in self.switches else
                      (self.controllers if node in self.controllers else [])))
        node.stop(deleteIntfs=True)
        node.terminate()
        nodes.remove(node)
        del self.nameToNode[ node.name ]

    def delHost(self, host):
        "Delete a host"
        self.delNode(host, nodes=self.hosts)

    def addStation(self, name, cls=None, **params):
        """Add Station.
           name: name of station to add
           cls: custom host class/constructor (optional)
           params: parameters for station
           returns: added station"""
        # Default IP and MAC addresses
        defaults = { 'ip': ipAdd(self.nextIP,
                                 ipBaseNum=self.ipBaseNum,
                                 prefixLen=self.prefixLen) +
                           '/%s' % self.prefixLen,
                     'channel': self.channel,
                     'mode': self.mode
                   }
        defaults.update(params)

        if self.autoSetPositions:
            defaults[ 'position' ] = ('%s,0,0' % self.nextPosition)
        if self.autoSetMacs:
            defaults[ 'mac' ] = macColonHex(self.nextIP)
        if self.autoPinCpus:
            defaults[ 'cores' ] = self.nextCore
            self.nextCore = (self.nextCore + 1) % self.numCores
        self.nextIP += 1
        self.nextPosition += 1

        if not cls:
            cls = self.station
        sta = cls(name, **defaults)

        mininetWiFi.addParameters(sta, self.autoSetMacs, defaults)

        if 'type' in params and params['type'] == 'ap':
            sta.cmd('ip link set lo up')
            sta.func[0] = 'ap'
            if 'ssid' in params:
                sta.params['ssid'] = []
                sta.params['ssid'].append('')
                sta.params['ssid'][0] = params['ssid']

        self.stations.append(sta)
        self.nameToNode[ name ] = sta

        return sta

    def addCar(self, name, cls=None, **params):
        """Add Car.
           name: name of vehicle to add
           cls: custom host class/constructor
           params: parameters for car
           returns: added car"""
        # Default IP and MAC addresses
        defaults = { 'ip': ipAdd(self.nextIP,
                                 ipBaseNum=self.ipBaseNum,
                                 prefixLen=self.prefixLen) +
                           '/%s' % self.prefixLen,
                     'channel': self.channel,
                     'mode': self.mode,
                     'ssid': self.ssid}

        if self.autoSetMacs:
            defaults[ 'mac' ] = macColonHex(self.nextIP)
        if self.autoPinCpus:
            defaults[ 'cores' ] = self.nextCore
            self.nextCore = (self.nextCore + 1) % self.numCores
        defaults.update(params)

        self.nextIP += 1
        if not cls:
            cls = self.car
        car = cls(name, **defaults)

        self.nameToNode[ name ] = car
        mininetWiFi.addParameters(car, self.autoSetMacs, defaults)

        carsta = self.addStation(name + 'STA', **defaults)
        carsta.pexec('ip link set lo up')
        car.params['carsta'] = carsta
        self.carsSTA.append(carsta)
        switchName = car.name + 'SW'
        carsw = self.addSwitch(switchName, inband=True)
        self.carsSW.append(carsw)
        self.cars.append(car)

        if 'func' in car.params and car.params['func'] == 'adhoc':
            car.params['ssid'] = 'adhoc-ssid'
            car.func.append('adhoc')
        else:
            car.params['ssid'] = 'mesh-ssid'
            car.func.append('mesh')
        mininetWiFi.isVanet = True
        return car

    def addAccessPoint(self, name, cls=None, **params):
        """Add AccessPoint.
           name: name of accesspoint to add
           cls: custom switch class/constructor (optional)
           returns: added acesspoint
           side effect: increments listenPort var ."""
        defaults = { 'listenPort': self.listenPort,
                     'inNamespace': self.inNamespace,
                     'ssid': self.ssid,
                     'channel': self.channel,
                     'mode': self.mode
                   }

        defaults.update(params)

        if self.autoSetPositions:
            defaults[ 'position' ] = ('0,%s,0' % self.nextPosition)
            self.nextPosition += 1

        if not cls:
            cls = self.accessPoint
        ap = cls(name, **defaults)

        if not self.inNamespace and self.listenPort:
            self.listenPort += 1

        if self.inNamespace or ('inNamespace' in params
                                and params['inNamespace'] is True):
            ap.params['inNamespace'] = True

        self.nameToNode[ name ] = ap

        mininetWiFi.addParameters(ap, self.autoSetMacs, defaults, mode='master')
        if 'type' in params and params['type'] == 'mesh':
            ap.func[1] = 'mesh'
            ap.ifaceToAssociate = 1

        self.aps.append(ap)

        return ap

    def addPhysicalBaseStation(self, name, cls=None, **params):
        """Add BaseStation.
           name: name of basestation to add
           cls: custom switch class/constructor (optional)
           returns: added physical basestation
           side effect: increments listenPort ivar ."""
        defaults = { 'listenPort': self.listenPort,
                     'inNamespace': self.inNamespace,
                     'channel': self.channel,
                     'mode': self.mode,
                     'ssid': self.ssid
                   }

        defaults.update(params)

        if not cls:
            cls = self.accessPoint
        bs = cls(name, **defaults)

        if not self.inNamespace and self.listenPort:
            self.listenPort += 1

        self.nameToNode[ name ] = bs

        wlan = ("%s" % params.pop('phywlan', {}))
        bs.params['phywlan'] = wlan
        self.aps.append(bs)

        mininetWiFi.addParameters(bs, self.autoSetMacs, defaults, mode='master')
        return bs

    def addSwitch(self, name, cls=None, **params):
        """Add switch.
           name: name of switch to add
           cls: custom switch class/constructor (optional)
           returns: added switch
           side effect: increments listenPort ivar ."""
        defaults = { 'listenPort': self.listenPort,
                     'inNamespace': self.inNamespace }

        defaults.update(params)

        if not cls:
            cls = self.switch

        sw = cls(name, **defaults)

        if not self.inNamespace and self.listenPort:
            self.listenPort += 1
        self.switches.append(sw)
        self.nameToNode[ name ] = sw
        return sw

    def delSwitch(self, switch):
        "Delete a switch"
        self.delNode(switch, nodes=self.switches)

    def addController(self, name='c0', controller=None, **params):
        """Add controller.
           controller: Controller class"""
        # Get controller class
        if not controller:
            controller = self.controller
        # Construct new controller if one is not given
        if isinstance(name, Controller):
            controller_new = name

            # Pylint thinks controller is a str()
            # pylint: disable=maybe-no-member
            name = controller_new.name
            # pylint: enable=maybe-no-member
        else:
            controller_new = controller(name, **params)
        # Add new controller to net
        if controller_new:  # allow controller-less setups
            self.controllers.append(controller_new)
            self.nameToNode[ name ] = controller_new
        return controller_new

    def delController(self, controller):
        """Delete a controller
           Warning - does not reconfigure switches, so they
           may still attempt to connect to it!"""
        self.delNode(controller)

    def addNAT(self, name='nat0', connect=True, inNamespace=False,
               **params):
        """Add a NAT to the Mininet network
           name: name of NAT node
           connect: switch to connect to | True (s1) | None
           inNamespace: create in a network namespace
           params: other NAT node params, notably:
               ip: used as default gateway address"""
        nat = self.addHost(name, cls=NAT, inNamespace=inNamespace,
                           subnet=self.ipBase, **params)
        # find first switch and create link
        if connect:
            if not isinstance(connect, Node):
                # Use first switch if not specified
                connect = self.switches[ 0 ]
            # Connect the nat to the switch
            self.addLink(nat, connect)
            # Set the default route on hosts
            natIP = nat.params[ 'ip' ].split('/')[ 0 ]
            for host in self.hosts:
                if host.inNamespace:
                    host.setDefaultRoute('via %s' % natIP)
        return nat

    # BL: We now have four ways to look up nodes
    # This may (should?) be cleaned up in the future.
    def getNodeByName(self, *args):
        "Return node(s) with given name(s)"
        if len(args) == 1:
            return self.nameToNode[ args[ 0 ] ]
        return [ self.nameToNode[ n ] for n in args ]

    def get(self, *args):
        "Convenience alias for getNodeByName"
        return self.getNodeByName(*args)

    # Even more convenient syntax for node lookup and iteration
    def __getitem__(self, key):
        "net[ name ] operator: Return node with given name"
        return self.nameToNode[ key ]

    def __delitem__(self, key):
        "del net[ name ] operator - delete node with given name"
        self.delNode(self.nameToNode[ key ])

    def __iter__(self):
        "return iterator over node names"
        for node in chain(self.hosts, self.switches, self.controllers,
                          self.stations, self.carsSTA, self.aps):
            yield node.name

    def __len__(self):
        "returns number of nodes in net"
        return (len(self.hosts) + len(self.switches) +
                len(self.controllers) + len(self.stations) +
                len(self.carsSTA) + len(self.aps))

    def __contains__(self, item):
        "returns True if net contains named node"
        return item in self.nameToNode

    def keys(self):
        "return a list of all node names or net's keys"
        return list(self)

    def values(self):
        "return a list of all nodes or net's values"
        return [ self[name] for name in self ]

    def items(self):
        "return (key,value) tuple list for every node in net"
        return zip(self.keys(), self.values())

    @staticmethod
    def randMac():
        "Return a random, non-multicast MAC address"
        return macColonHex(random.randint(1, 2 ** 48 - 1) & 0xfeffffffffff |
                           0x020000000000)

    @classmethod
    def runAlternativeModule(cls, moduleDir):
        "Run an alternative module rather than mac80211_hwsim"
        mininetWiFi.alternativeModule = moduleDir

    def addMesh(self, node, **params):
        """
        Configure wireless mesh
        
        node: name of the node
        cls: custom association class/constructor
        params: parameters for node
        """
        params['nextIP'] = self.nextIP
        params['ipBaseNum'] = self.ipBaseNum
        params['prefixLen'] = self.prefixLen
        mininetWiFi.addMesh(node, **params)

    def addHoc(self, node, **params):
        """
        Configure AdHoc
        
        node: name of the node
        cls: custom association class/constructor
        params: parameters for station
           
        """
        params['nextIP'] = self.nextIP
        params['ipBaseNum'] = self.ipBaseNum
        params['prefixLen'] = self.prefixLen
        mininetWiFi.addHoc(node, **params)

    @classmethod
    def wifiDirect(cls, node, **params):
        """
        Configure wifidirect
        
        node: name of node
        params: parameters for station
        """
        mininetWiFi.wifiDirect(node, **params)

    @classmethod
    def useIFB(cls):
        "Support to Intermediate Functional Block (IFB) Devices"
        mininetWiFi.ifb = True

    def addLink(self, node1, node2, port1=None, port2=None,
                cls=None, **params):
        """"Add a link from node1 to node2
            node1: source node (or name)
            node2: dest node (or name)
            port1: source port (optional)
            port2: dest port (optional)
            cls: link class (optional)
            params: additional link params (optional)
            returns: link object"""
        # Accept node objects or names
        node1 = node1 if not isinstance(node1, basestring) else self[ node1 ]
        node2 = node2 if not isinstance(node2, basestring) else self[ node2 ]
        options = dict(params)

        mininetWiFi.connections.setdefault('src', [])
        mininetWiFi.connections.setdefault('dst', [])
        mininetWiFi.connections.setdefault('ls', [])

        # If AP and STA
        if ((((isinstance(node1, Station) or isinstance(node1, Car))
              and ('ssid' in node2.params and 'apsInRange' in node1.params))
             or ((isinstance(node2, Station) or isinstance(node2, Car))
                 and ('ssid' in node1.params and 'apsInRange' in node2.params)))
            and 'link' not in options):

            sta_intf = None
            ap_intf = 0
            if (isinstance(node1, Station) or isinstance(node1, Car)) \
                    and 'ssid' in node2.params:
                sta = node1
                ap = node2
                if port1:
                    sta_intf = port1
                if port2:
                    ap_intf = port2
            else:
                sta = node2
                ap = node1
                if port2:
                    sta_intf = port2
                if port1:
                    ap_intf = port1

            if sta_intf:
                sta_wlan = sta_intf
            else:
                sta_wlan = sta.ifaceToAssociate
            ap_wlan = ap_intf

            sta.params['mode'][sta_wlan] = ap.params['mode'][ap_wlan]
            sta.params['channel'][sta_wlan] = ap.params['channel'][ap_wlan]

            # If sta/ap have defined position
            if 'position' in sta.params and 'position' in ap.params:
                dist = sta.get_distance_to(ap)
                if dist > ap.params['range'][0]:
                    doAssociation = False
                else:
                    doAssociation = True
            else:
                doAssociation = True

            if doAssociation:
                if self.enable_interference:
                    wmediumd_mode = 'interference'
                elif self.enable_error_prob:
                    wmediumd_mode = 'error_prob'
                else:
                    wmediumd_mode = None
                cls = Association
                cls.associate(sta, ap, self.enable_wmediumd,
                              wmediumd_mode, sta_wlan, ap_wlan)
                if 'bw' not in params and 'mininet.util.TCIntfWireless' \
                        not in str(self.link):
                    value = mininetWiFi.setDataRate(sta, ap, sta_wlan)
                    bw = value.rate
                    params['bw'] = bw

                if 'mininet.util.TCIntfWireless' in str(self.link):
                    cls = self.link
                else:
                    cls = TCIntfWireless

                if 'mininet.util.TCIntfWireless' in str(self.link) \
                        or 'bw' in params and params['bw'] != 0 \
                    and 'position' not in sta.params:
                    # tc = True, this is useful only to apply tc configuration
                    if not mininetWiFi.enable_interference:
                        cls(name=sta.params['wlan'][sta_wlan], node=sta,
                            link=None, tc=True, **params)
            if self.enable_wmediumd:
                if self.enable_error_prob:
                    mininetWiFi.wlinks.append([sta, ap, params['error_prob']])
                elif not self.enable_interference:
                    mininetWiFi.wlinks.append([sta, ap])

        elif (isinstance(node1, AP) and isinstance(node2, AP)
              and 'link' in options and options['link'] == '4addr'):
            # If sta/ap have defined position
            if 'position' in node1.params and 'position' in node2.params:
                mininetWiFi.connections['src'].append(node1)
                mininetWiFi.connections['dst'].append(node2)
                mininetWiFi.connections['ls'].append('--')
            if mininetWiFi.enable_interference:
                cls = _4addrLink
                cls(node1, node2)
            else:
                dist = node1.get_distance_to(node2)
                if dist > node1.params['range'][0]:
                    doAssociation = False
                else:
                    doAssociation = True

                if doAssociation:
                    cls = _4addrLink
                    cls(node1, node2)
        else:
            if 'link' in options:
                options.pop('link', None)

            if 'position' in node1.params and 'position' in node2.params or \
                    (isinstance(node1, AP) and isinstance(node2, AP) and mininetWiFi.isVanet):
                mininetWiFi.connections['src'].append(node1)
                mininetWiFi.connections['dst'].append(node2)
                mininetWiFi.connections['ls'].append('-')
            # Port is optional
            if port1 is not None:
                options.setdefault('port1', port1)
            if port2 is not None:
                options.setdefault('port2', port2)

            # Set default MAC - this should probably be in Link
            options.setdefault('addr1', self.randMac())
            options.setdefault('addr2', self.randMac())

            cls = self.link if cls is None else cls
            link = cls(node1, node2, **options)
            self.links.append(link)
            return link

    def autoAssociation(self):
        "This is useful to make the users' life easier"
        mininetWiFi.autoAssociation(self.stations, self.aps)

    def configureWifiNodes(self):
        "Configure WiFi Nodes"
        self.isWiFi = True
        if not self.ppm_is_set:
            self.propagationModel()
        self.stations, self.aps = mininetWiFi.configureWifiNodes(self)

    def delLink(self, link):
        "Remove a link from this network"
        link.delete()
        self.links.remove(link)

    def linksBetween(self, node1, node2):
        "Return Links between node1 and node2"
        return [ link for link in self.links
                 if (node1, node2) in (
                     (link.intf1.node, link.intf2.node),
                     (link.intf2.node, link.intf1.node)) ]

    def delLinkBetween(self, node1, node2, index=0, allLinks=False):
        """Delete link(s) between node1 and node2
           index: index of link to delete if multiple links (0)
           allLinks: ignore index and delete all such links (False)
           returns: deleted link(s)"""
        links = self.linksBetween(node1, node2)
        if not allLinks:
            links = [ links[ index ] ]
        for link in links:
            self.delLink(link)
        return links

    def configHosts(self):
        "Configure a set of hosts."
        hosts = self.hosts + self.stations
        for host in hosts:
            # info( host.name + ' ' )
            intf = host.defaultIntf()
            if intf:
                host.configDefault()
            else:
                # Don't configure nonexistent intf
                host.configDefault(ip=None, mac=None)
            # You're low priority, dude!
            # BL: do we want to do this here or not?
            # May not make sense if we have CPU lmiting...
            # quietRun( 'renice +18 -p ' + repr( host.pid ) )
            # This may not be the right place to do this, but
            # it needs to be done somewhere.
        # info( '\n' )

    def buildFromTopo(self, topo=None):
        """Build mininet from a topology object
           At the end of this function, everything should be connected
           and up."""
        cls = Association
        cls.printCon = False
        # Possibly we should clean up here and/or validate
        # the topo
        if self.cleanup:
            pass

        info('*** Creating network\n')
        if not self.controllers and self.controller:
            # Add a default controller
            info('*** Adding controller\n')
            classes = self.controller
            if not isinstance(classes, list):
                classes = [ classes ]
            for i, cls in enumerate(classes):
                # Allow Controller objects because nobody understands partial()
                if isinstance(cls, Controller):
                    self.addController(cls)
                else:
                    self.addController('c%d' % i, cls)

        info('*** Adding hosts and stations:\n')
        for hostName in topo.hosts():
            if 'sta' in str(hostName):
                self.addStation(hostName, **topo.nodeInfo(hostName))
            else:
                self.addHost(hostName, **topo.nodeInfo(hostName))
            info(hostName + ' ')

        info('\n*** Adding switches and access point(s):\n')
        for switchName in topo.switches():
            # A bit ugly: add batch parameter if appropriate
            params = topo.nodeInfo(switchName)
            cls = params.get('cls', self.switch)
            if hasattr(cls, 'batchStartup'):
                params.setdefault('batch', True)
            if 'ap' in str(switchName):
                self.addAccessPoint(switchName, **params)
            else:
                self.addSwitch(switchName, **params)
            info(switchName + ' ')

        if self.isWiFi:
            info('\n*** Configuring wifi nodes...\n')
            self.configureWifiNodes()

        info('\n*** Adding link(s):\n')
        for srcName, dstName, params in topo.links(
                sort=True, withInfo=True):
            self.addLink(**params)
            info('(%s, %s) ' % (srcName, dstName))
        info('\n')

    def configureControlNetwork(self):
        "Control net config hook: override in subclass"
        raise Exception('configureControlNetwork: '
                        'should be overriden in subclass', self)

    def create_vanet_link(self):
        for idx, car in enumerate(self.cars):
            self.addLink(self.carsSTA[idx], self.carsSW[idx])
            self.addLink(car, self.carsSW[idx])

    def build(self):
        "Build mininet."
        if self.topo:
            self.buildFromTopo(self.topo)

        if mininetWiFi.isVanet:
            self.create_vanet_link()

        if (self.configure4addr or self.configureWiFiDirect or self.enable_error_prob) \
                and self.enable_wmediumd:
            mininetWiFi.configureWmediumd(self)
            mininetWiFi.wmediumdConnect()

        if self.inNamespace:
            self.configureControlNetwork()
        info('*** Configuring nodes\n')
        self.configHosts()
        if self.xterms:
            self.startTerms()
        if self.autoStaticArp:
            self.staticArp()

        isPosDefined = False
        nodes = self.stations
        for node in nodes:
            if 'position' in node.params:
                isPosDefined = True
            for wlan in range(0, len(node.params['wlan'])):
                if not isinstance(node, AP) and node.func[0] != 'ap' and \
                    node.func[wlan] != 'mesh' and node.func[wlan] != 'adhoc' \
                        and node.func[wlan] != 'wifiDirect':
                    if not node.autoTxPower:
                        node.params['range'][wlan] = \
                                int(node.params['range'][wlan])/5

        if self.isWiFi and not self.disableAutoAssociation \
                and not self.isMobility:
            mininetWiFi.autoAssociation(self.stations, self.aps)

        if self.isMobility:
            if self.isMobilityModel or mininetWiFi.isVanet:
                self.mobilityKwargs['mobileNodes'] = self.getMobileNodes()
                mininetWiFi.start_mobility(**self.mobilityKwargs)
            else:
                self.mobilityKwargs['plotNodes'] = self.plot_nodes()
                mininetWiFi.stopMobility(**self.mobilityKwargs)

        if not self.isMobility \
                and mininetWiFi.prop_model_name == \
                        'logNormalShadowing':
            import threading
            thread = threading.Thread(target=mininetWiFi.plotCheck,
                                      args=(self.stations, self.aps))
            thread.daemon = True
            thread.start()
        else:
            if not self.isMobility and mininetWiFi.DRAW \
                    and not mininetWiFi.alreadyPlotted and isPosDefined:
                plotNodes = self.plot_nodes()
                self.stations, self.aps = \
                    mininetWiFi.plotCheck(self.stations, self.aps,
                                          plotNodes)
        self.built = True

    def plot_nodes(self):
        other_nodes = self.hosts + self.switches + self.controllers
        plotNodes = []
        for node in other_nodes:
            if hasattr(node, 'plot') and node.plot == True:
                plotNodes.append(node)
        return plotNodes

    def startTerms(self):
        "Start a terminal for each node."
        if 'DISPLAY' not in os.environ:
            error("Error starting terms: Cannot connect to display\n")
            return
        info("*** Running terms on %s\n" % os.environ[ 'DISPLAY' ])
        cleanUpScreens()
        self.terms += makeTerms(self.controllers, 'controller')
        self.terms += makeTerms(self.switches, 'switch')
        self.terms += makeTerms(self.hosts, 'host')
        self.terms += makeTerms(self.stations, 'station')
        self.terms += makeTerms(self.aps, 'ap')

    def stopXterms(self):
        "Kill each xterm."
        for term in self.terms:
            os.kill(term.pid, signal.SIGKILL)
        cleanUpScreens()

    def staticArp(self):
        "Add all-pairs ARP entries to remove the need to handle broadcast."
        for src in self.hosts:
            for dst in self.hosts:
                if src != dst:
                    src.setARP(ip=dst.IP(), mac=dst.MAC())

    def start(self):
        "Start controller and switches."
        if not self.built:
            self.build()
        info('*** Starting controller(s)\n')
        for controller in self.controllers:
            info(controller.name + ' ')
            controller.start()
        info('\n')

        info('*** Starting switches and/or access points\n')
        nodesL2 = self.switches + self.aps
        for switch in nodesL2:
            info(switch.name + ' ')
            switch.start(self.controllers)

        started = {}
        for swclass, switches in groupby(
                sorted(nodesL2, key=type), type):
            switches = tuple(switches)
            if hasattr(swclass, 'batchStartup'):
                success = swclass.batchStartup(switches)
                started.update({ s: s for s in success })
        info('\n')
        if self.waitConn:
            self.waitConnected()

    @classmethod
    def seed(cls, seed):
        "Seed"
        kwargs = dict()
        kwargs['seed'] = seed
        mininetWiFi.setMobilityParams(**kwargs)

    @classmethod
    def roads(cls, nroads):
        "Number of roads"
        mininetWiFi.nroads = nroads

    def stop(self):
        if self.isWiFi:
            mininetWiFi.stopGraphParams()
        info('*** Stopping %i controllers\n' % len(self.controllers))
        for controller in self.controllers:
            info(controller.name + ' ')
            controller.stop()
        info('\n')
        if self.terms:
            info('*** Stopping %i terms\n' % len(self.terms))
            self.stopXterms()
        info('*** Stopping %i links\n' % len(self.links))
        for link in self.links:
            info('.')
            link.stop()
        info('\n')
        info('*** Stopping switches/access points\n')
        stopped = {}
        nodesL2 = self.switches + self.aps
        for swclass, switches in groupby(
                sorted(nodesL2, key=type), type):
            switches = tuple(switches)
            if hasattr(swclass, 'batchShutdown'):
                success = swclass.batchShutdown(switches)
                stopped.update({ s: s for s in success })
        for switch in nodesL2:
            info(switch.name + ' ')
            if switch not in stopped:
                switch.stop()
            switch.terminate()
        info('\n')
        info('*** Stopping hosts/stations\n')
        hosts = self.hosts + self.stations
        for host in hosts:
            info(host.name + ' ')
            host.terminate()
        info('\n')
        if self.aps is not []:
            mininetWiFi.kill_hostapd()
        if self.isWiFi:
            mininetWiFi.closeMininetWiFi()
        if self.enable_wmediumd:
            mininetWiFi.kill_wmediumd()
        info('\n*** Done\n')

    def run(self, test, *args, **kwargs):
        "Perform a complete start/test/stop cycle."
        self.start()
        info('*** Running test\n')
        result = test(*args, **kwargs)
        self.stop()
        return result

    def monitor(self, hosts=None, timeoutms=-1):
        """Monitor a set of hosts (or all hosts by default),
           and return their output, a line at a time.
           hosts: (optional) set of hosts to monitor
           timeoutms: (optional) timeout value in ms
           returns: iterator which returns host, line"""
        if hosts is None:
            hosts = self.hosts
        poller = select.poll()
        h1 = hosts[ 0 ]  # so we can call class method fdToNode
        for host in hosts:
            poller.register(host.stdout)
        while True:
            ready = poller.poll(timeoutms)
            for fd, event in ready:
                host = h1.fdToNode(fd)
                if event & select.POLLIN:
                    line = host.readline()
                    if line is not None:
                        yield host, line
            # Return if non-blocking
            if not ready and timeoutms >= 0:
                yield None, None

    @staticmethod
    def _parsePing(pingOutput):
        "Parse ping output and return packets sent, received."
        # Check for downed link
        if 'connect: Network is unreachable' in pingOutput:
            return 1, 0
        r = r'(\d+) packets transmitted, (\d+)( packets)? received'
        m = re.search(r, pingOutput)
        if m is None:
            error('*** Error: could not parse ping output: %s\n' %
                  pingOutput)
            return 1, 0
        sent, received = int(m.group(1)), int(m.group(2))
        return sent, received

    def ping(self, hosts=None, timeout=None):
        """Ping between all specified hosts.
           hosts: list of hosts
           timeout: time to wait for a response, as string
           returns: ploss packet loss percentage"""
        # should we check if running?
        packets = 0
        lost = 0
        ploss = None
        if not hosts:
            hosts = self.hosts + self.stations
            output('*** Ping: testing ping reachability\n')
        for node in hosts:
            output('%s -> ' % node.name)
            for dest in hosts:
                if node != dest:
                    opts = ''
                    if timeout:
                        opts = '-W %s' % timeout
                    if dest.intfs:
                        result = node.cmdPrint('ping -c1 %s %s'
                                               % (opts, dest.IP()))
                        sent, received = self._parsePing(result)
                    else:
                        sent, received = 0, 0
                    packets += sent
                    if received > sent:
                        error('*** Error: received too many packets')
                        error('%s' % result)
                        node.cmdPrint('route')
                        exit(1)
                    lost += sent - received
                    output(('%s ' % dest.name) if received else 'X ')
            output('\n')
        if packets > 0:
            ploss = 100.0 * lost / packets
            received = packets - lost
            output("*** Results: %i%% dropped (%d/%d received)\n" %
                   (ploss, received, packets))
        else:
            ploss = 0
            output("*** Warning: No packets sent\n")
        return ploss

    @staticmethod
    def _parseFull(pingOutput):
        "Parse ping output and return all data."
        errorTuple = (1, 0, 0, 0, 0, 0)
        # Check for downed link
        r = r'[uU]nreachable'
        m = re.search(r, pingOutput)
        if m is not None:
            return errorTuple
        r = r'(\d+) packets transmitted, (\d+)( packets)? received'
        m = re.search(r, pingOutput)
        if m is None:
            error('*** Error: could not parse ping output: %s\n' %
                  pingOutput)
            return errorTuple
        sent, received = int(m.group(1)), int(m.group(2))
        r = r'rtt min/avg/max/mdev = '
        r += r'(\d+\.\d+)/(\d+\.\d+)/(\d+\.\d+)/(\d+\.\d+) ms'
        m = re.search(r, pingOutput)
        if m is None:
            if received == 0:
                return errorTuple
            error('*** Error: could not parse ping output: %s\n' %
                  pingOutput)
            return errorTuple
        rttmin = float(m.group(1))
        rttavg = float(m.group(2))
        rttmax = float(m.group(3))
        rttdev = float(m.group(4))
        return sent, received, rttmin, rttavg, rttmax, rttdev

    def pingFull(self, hosts=None, timeout=None):
        """Ping between all specified hosts and return all data.
           hosts: list of hosts
           timeout: time to wait for a response, as string
           returns: all ping data; see function body."""
        # should we check if running?
        # Each value is a tuple: (src, dsd, [all ping outputs])
        all_outputs = []
        if not hosts:
            hosts = self.hosts
            output('*** Ping: testing ping reachability\n')
        for node in hosts:
            output('%s -> ' % node.name)
            for dest in hosts:
                if node != dest:
                    opts = ''
                    if timeout:
                        opts = '-W %s' % timeout
                    result = node.cmd('ping -c1 %s %s' % (opts, dest.IP()))
                    outputs = self._parsePingFull(result)
                    sent, received, rttmin, rttavg, rttmax, rttdev = outputs
                    all_outputs.append((node, dest, outputs))
                    output(('%s ' % dest.name) if received else 'X ')
            output('\n')
        output("*** Results: \n")
        for outputs in all_outputs:
            src, dest, ping_outputs = outputs
            sent, received, rttmin, rttavg, rttmax, rttdev = ping_outputs
            output(" %s->%s: %s/%s, " % (src, dest, sent, received))
            output("rtt min/avg/max/mdev %0.3f/%0.3f/%0.3f/%0.3f ms\n" %
                   (rttmin, rttavg, rttmax, rttdev))
        return all_outputs

    def pingAll(self, timeout=None):
        """Ping between all hosts.
           returns: ploss packet loss percentage"""
        if self.isWiFi:
            sleep(1)
        return self.ping(timeout=timeout)

    def pingPair(self):
        """Ping between first two hosts, useful for testing.
           returns: ploss packet loss percentage"""
        if self.isWiFi:
            sleep(1)
        nodes = self.hosts + self.stations
        hosts = [ nodes[ 0 ], nodes[ 1 ] ]
        return self.ping(hosts=hosts)

    def pingAllFull(self):
        """Ping between all hosts.
           returns: ploss packet loss percentage"""
        if self.isWiFi:
            sleep(1)
        return self.pingFull()

    def pingPairFull(self):
        """Ping between first two hosts, useful for testing.
           returns: ploss packet loss percentage"""
        if self.isWiFi:
            sleep(1)
        nodes = self.hosts + self.stations
        hosts = [ nodes[ 0 ], nodes[ 1 ] ]
        return self.pingFull(hosts=hosts)

    @staticmethod
    def _parseIperf(iperfOutput):
        """Parse iperf output and return bandwidth.
           iperfOutput: string
           returns: result string"""
        r = r'([\d\.]+ \w+/sec)'
        m = re.findall(r, iperfOutput)
        if m:
            return m[-1]
        else:
            # was: raise Exception(...)
            error('could not parse iperf output: ' + iperfOutput)
            return ''

    def iperf(self, hosts=None, l4Type='TCP', udpBw='10M', fmt=None,
              seconds=5, port=5001):
        """Run iperf between two hosts.
           hosts: list of hosts; if None, uses first and last hosts
           l4Type: string, one of [ TCP, UDP ]
           udpBw: bandwidth target for UDP test
           fmt: iperf format argument if any
           seconds: iperf time to transmit
           port: iperf port
           returns: two-element array of [ server, client ] speeds
           note: send() is buffered, so client rate can be much higher than
           the actual transmission rate; on an unloaded system, server
           rate should be much closer to the actual receive rate"""
        if self.isWiFi:
            sleep(1)
        nodes = self.hosts + self.stations
        hosts = hosts or [ nodes[ 0 ], nodes[ -1 ] ]
        assert len(hosts) == 2
        client, server = hosts

        conn1 = 0
        conn2 = 0
        if isinstance(client, Station) or isinstance(server, Station):
            if isinstance(client, Station):
                while conn1 == 0:
                    conn1 = int(client.cmd('iw dev %s link '
                                           '| grep -ic \'Connected\''
                                           % client.params['wlan'][0]))
            if isinstance(server, Station):
                while conn2 == 0:
                    conn2 = int(server.cmd('iw dev %s link | grep -ic '
                                           '\'Connected\''
                                           % server.params['wlan'][0]))
        output('*** Iperf: testing', l4Type, 'bandwidth between',
               client, 'and', server, '\n')
        server.cmd('killall -9 iperf')
        iperfArgs = 'iperf -p %d ' % port
        bwArgs = ''
        if l4Type == 'UDP':
            iperfArgs += '-u '
            bwArgs = '-b ' + udpBw + ' '
        elif l4Type != 'TCP':
            raise Exception('Unexpected l4 type: %s' % l4Type)
        if fmt:
            iperfArgs += '-f %s ' % fmt
        server.sendCmd(iperfArgs + '-s')
        if l4Type == 'TCP':
            if not waitListening(client, server.IP(), port):
                raise Exception('Could not connect to iperf on port %d'
                                % port)
        cliout = client.cmd(iperfArgs + '-t %d -c ' % seconds +
                            server.IP() + ' ' + bwArgs)
        debug('Client output: %s\n' % cliout)
        servout = ''
        # We want the last *b/sec from the iperf server output
        # for TCP, there are two of them because of waitListening
        count = 2 if l4Type == 'TCP' else 1
        while len(re.findall('/sec', servout)) < count:
            servout += server.monitor(timeoutms=5000)
        server.sendInt()
        servout += server.waitOutput()
        debug('Server output: %s\n' % servout)
        result = [ self._parseIperf(servout), self._parseIperf(cliout) ]
        if l4Type == 'UDP':
            result.insert(0, udpBw)
        output('*** Results: %s\n' % result)
        return result

    def runCpuLimitTest(self, cpu, duration=5):
        """run CPU limit test with 'while true' processes.
        cpu: desired CPU fraction of each host
        duration: test duration in seconds (integer)
        returns a single list of measured CPU fractions as floats.
        """
        cores = int(quietRun('nproc'))
        pct = cpu * 100
        info('*** Testing CPU %.0f%% bandwidth limit\n' % pct)
        hosts = self.hosts
        cores = int(quietRun('nproc'))
        # number of processes to run a while loop on per host
        num_procs = int(ceil(cores * cpu))
        pids = {}
        for h in hosts:
            pids[ h ] = []
            for _core in range(num_procs):
                h.cmd('while true; do a=1; done &')
                pids[ h ].append(h.cmd('echo $!').strip())
        outputs = {}
        time = {}
        # get the initial cpu time for each host
        for host in hosts:
            outputs[ host ] = []
            with open('/sys/fs/cgroup/cpuacct/%s/cpuacct.usage'
                      % host, 'r') as f:
                time[ host ] = float(f.read())
        for _ in range(duration):
            sleep(1)
            for host in hosts:
                with open('/sys/fs/cgroup/cpuacct/%s/cpuacct.usage'
                          % host, 'r') as f:
                    readTime = float(f.read())
                outputs[ host ].append(((readTime - time[ host ])
                                        / 1000000000) / cores * 100)
                time[ host ] = readTime
        for h, pids in pids.items():
            for pid in pids:
                h.cmd('kill -9 %s' % pid)
        cpu_fractions = []
        for _host, outputs in outputs.items():
            for pct in outputs:
                cpu_fractions.append(pct)
        output('*** Results: %s\n' % cpu_fractions)
        return cpu_fractions

    def get_distance(self, src, dst):
        """Gets the distance between two nodes

        :params src: source node
        :params dst: destination node
        :params nodes: list of nodes"""
        nodes = self.stations + self.cars + self.aps
        try:
            for host1 in nodes:
                if src == str(host1):
                    src = host1
                    for host2 in nodes:
                        if dst == str(host2):
                            dst = host2
                            dist = src.get_distance_to(dst)
                            info("The distance between %s and %s is %.2f "
                                 "meters\n" % (src, dst, float(dist)))
        except:
            info("node %s or/and node %s does not exist or there is no " \
            "position defined" % (dst, src))

    @classmethod
    def mobility(cls, *args, **kwargs):
        "Configure mobility parameters"
        mininetWiFi.configureMobility(*args, **kwargs)

    def startMobility(self, **kwargs):
        "Starts Mobility"
        self.isMobility = True
        if 'repetitions' in kwargs:
            self.repetitions = kwargs['repetitions']
        if 'model' in kwargs:
            self.isMobilityModel = True
            kwargs['mobileNodes'] = self.getMobileNodes()
        self.mobilityKwargs = kwargs
        kwargs['stations'] = self.stations
        kwargs['aps'] = self.aps
        mininetWiFi.setMobilityParams(**kwargs)

    def stopMobility(self, **kwargs):
        """Stops Mobility"""
        self.mobilityKwargs.update(kwargs)
        mininetWiFi.setMobilityParams(**kwargs)

    def getMobileNodes(self):
        mobileNodes = []
        nodes = self.stations + self.aps + self.cars
        for node in nodes:
            if 'position' not in node.params:
                mobileNodes.append(node)
        return mobileNodes

    def useExternalProgram(self, program, **params):
        """
        Opens an external program
        
        :params program: any program (useful for SUMO)
        :params **params config_file: file configuration
        """
        params['stations'] = self.stations
        params['aps'] = self.aps
        params['cars'] = self.cars
        params['program'] = program
        self.disableAutoAssociation = True
        mininetWiFi.useExternalProgram(**params)

    @classmethod
    def setBgscan(cls, module='simple', s_inverval=30, signal=-45,
                  l_interval=300, database='/etc/wpa_supplicant/network1.bgscan'):
        """
        Set Background scanning

        :params module: module
        :params s_inverval: short bgscan interval in second
        :params signal: signal strength threshold
        :params l_interval: long interval
        :params database: database file name
        """
        if module == 'simple':
            bgscan = 'bgscan=\"%s:%d:%d:%d\"' % \
                     (module, s_inverval, signal, l_interval)
        else:
            bgscan = 'bgscan=\"%s:%d:%d:%d:%s\"' % \
                     (module, s_inverval, signal, l_interval, database)

        Association.bgscan = bgscan

    def defaultGraph(self):
        "Default values for graph"
        self.plotGraph(min_x=0, min_y=0, min_z=0, max_x=100, max_y=100, max_z=0)

    def stop_simulation(self):
        mininetWiFi.stop_simulation()

    def start_simulation(self):
        mininetWiFi.start_simulation()

    @classmethod
    def plotGraph(cls, min_x=0, min_y=0, min_z=0, max_x=0, max_y=0, max_z=0):
        """ 
        Plots Graph 
        
        :params min_x: minimum X
        :params min_y: minimum Y
        :params min_z: minimum Z
        :params max_x: maximum X
        :params max_y: maximum Y
        :params max_z: maximum Z
        """
        mininetWiFi.plotGraph(min_x, min_y, min_z, max_x, max_y, max_z)

    @classmethod
    def setChannelEquation(cls, **params):
        """ 
        Set Channel Equation.
        
        :params bw: bandwidth (mbps)
        :params delay: delay (ms)
        :params latency: latency (ms)
        :params loss: loss (%)
        """
        mininetWiFi.setChannelEquation(**params)

    def propagationModel(self, **kwargs):
        """ 
        Propagation Model Attr
        """
        self.ppm_is_set = True
        kwargs['noise_threshold'] = self.noise_threshold
        kwargs['cca_threshold'] = self.cca_threshold
        mininetWiFi.propagation_model(**kwargs)

    @classmethod
    def associationControl(cls, ac):
        """
        Defines an association control
        
        :params ac: the association control
        """
        mininetWiFi.associationControl(ac)

    def configWirelessLinkStatus(self, src, dst, status):
        if status == 'down':
            if isinstance(self.nameToNode[ src ], Station):
                sta = self.nameToNode[ src ]
                ap = self.nameToNode[ dst ]
            else:
                sta = self.nameToNode[ dst ]
                ap = self.nameToNode[ src ]
            for wlan in range(0, len(sta.params['wlan'])):
                if sta.params['associatedTo'][wlan] != '':
                    sta.cmd('iw dev %s disconnect' % sta.params['wlan'][wlan])
                    sta.params['associatedTo'][wlan] = ''
                    ap.params['associatedStations'].remove(sta)
        else:
            if isinstance(self.nameToNode[ src ], Station):
                sta = self.nameToNode[ src ]
                ap = self.nameToNode[ dst ]
            else:
                sta = self.nameToNode[ dst ]
                ap = self.nameToNode[ src ]
            for wlan in range(0, len(sta.params['wlan'])):
                if sta.params['associatedTo'][wlan] == '':
                    sta.pexec('iw dev %s connect %s %s'
                              % (sta.params['wlan'][wlan],
                                 ap.params['ssid'][0], ap.params['mac'][0]))
                    sta.params['associatedTo'][wlan] = ap
                    ap.params['associatedStations'].append(sta)

    # BL: I think this can be rewritten now that we have
    # a real link class.
    def configLinkStatus(self, src, dst, status):
        """Change status of src <-> dst links.
           src: node name
           dst: node name
           status: string {up, down}"""
        if src not in self.nameToNode:
            error('src not in network: %s\n' % src)
        elif dst not in self.nameToNode:
            error('dst not in network: %s\n' % dst)
        if isinstance(self.nameToNode[ src ], Station) \
                and isinstance(self.nameToNode[ dst ], AP) or \
                            isinstance(self.nameToNode[src], AP) \
                        and isinstance(self.nameToNode[dst], Station):
            self.configWirelessLinkStatus(src, dst, status)
        else:
            src = self.nameToNode[ src ]
            dst = self.nameToNode[ dst ]
            connections = src.connectionsTo(dst)
            if len(connections) == 0:
                error('src and dst not connected: %s %s\n' % (src, dst))
            for srcIntf, dstIntf in connections:
                result = srcIntf.ipLink(status)
                if result:
                    error('link src status change failed: %s\n' % result)
                result = dstIntf.ipLink(status)
                if result:
                    error('link dst status change failed: %s\n' % result)

    def interact(self):
        "Start network and run our simple CLI."
        self.start()
        result = CLI(self)
        self.stop()
        return result

    inited = False

    @classmethod
    def init(cls):
        "Initialize Mininet"
        if cls.inited:
            return
        ensureRoot()
        fixLimits()
        cls.inited = True


class MininetWithControlNet(Mininet):

    """Control network support:

       Create an explicit control network. Currently this is only
       used/usable with the user datapath.

       Notes:

       1. If the controller and switches are in the same (e.g. root)
          namespace, they can just use the loopback connection.

       2. If we can get unix domain sockets to work, we can use them
          instead of an explicit control network.

       3. Instead of routing, we could bridge or use 'in-band' control.

       4. Even if we dispense with this in general, it could still be
          useful for people who wish to simulate a separate control
          network (since real networks may need one!)

       5. Basically nobody ever used this code, so it has been moved
          into its own class.

       6. Ultimately we may wish to extend this to allow us to create a
          control network which every node's control interface is
          attached to."""

    def configureControlNetwork(self):
        "Configure control network."
        self.configureRoutedControlNetwork()

    # We still need to figure out the right way to pass
    # in the control network location.

    def configureRoutedControlNetwork(self, ip='192.168.123.1',
                                      prefixLen=16):
        """Configure a routed control network on controller and switches.
           For use with the user datapath only right now."""
        controller = self.controllers[ 0 ]
        info(controller.name + ' <->')
        cip = ip
        snum = ipParse(ip)
        nodesL2 = self.switches + self.aps
        for switch in nodesL2:
            info(' ' + switch.name)
            link = self.link(switch, controller, port1=0)
            sintf, cintf = link.intf1, link.intf2
            switch.controlIntf = sintf
            snum += 1
            while snum & 0xff in [ 0, 255 ]:
                snum += 1
            sip = ipStr(snum)
            cintf.setIP(cip, prefixLen)
            sintf.setIP(sip, prefixLen)
            controller.setHostRoute(sip, cintf)
            switch.setHostRoute(cip, sintf)
        info('\n')
        info('*** Testing control network\n')
        while not cintf.isUp():
            info('*** Waiting for', cintf, 'to come up\n')
            sleep(1)
        for switch in nodesL2:
            while not sintf.isUp():
                info('*** Waiting for', sintf, 'to come up\n')
                sleep(1)
            if self.ping(hosts=[ switch, controller ]) != 0:
                error('*** Error: control network test failed\n')
                exit(1)
        info('\n')
