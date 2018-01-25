from mininet.log import debug
from mininet.wifiLink import wirelessLink

class associationControl (object):

    changeAP = False

    def __init__(self, sta, ap, wlan, ac):
        self.customAssociationControl(sta, ap, wlan, ac)

    def customAssociationControl(self, sta, ap, wlan, ac):
        """Mechanisms that optimize the use of the APs
        llf: Least-loaded-first
        ssf: Strongest-signal-first"""
        if ac == "llf":
            apref = sta.params['associatedTo'][wlan]
            if apref != '':
                ref_llf = len(apref.params['associatedStations'])
                if len(ap.params['associatedStations']) + 2 < ref_llf:
                    debug('iw dev %s disconnect' % sta.params['wlan'][wlan])
                    sta.pexec('iw dev %s disconnect' % sta.params['wlan'][wlan])
                    self.changeAP = True
            else:
                self.changeAP = True
        elif ac == "ssf":
            rssi = sta.params['rssi'][wlan]
            ref_dist = sta.get_distance_to(ap)
            ref_rssi = wirelessLink.setRSSI(sta, ap, wlan, ref_dist)
            if float(ref_rssi) > float(rssi + 0.1):
                debug('iw dev %s disconnect' % sta.params['wlan'][wlan])
                sta.pexec('iw dev %s disconnect' % sta.params['wlan'][wlan])
                self.changeAP = True
        return self.changeAP
