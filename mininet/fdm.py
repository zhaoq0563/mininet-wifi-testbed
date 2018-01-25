from mininet.log import info, output
from mininet.wifiLink import wirelessLink
import threading
import time

def getNextIP(ip):
    last=ip.split('.')[-1]
    base=ip.rstrip(last)
    new=int(last)+1
    return base+str(new)

class FDM(object):
    '''Constants'''
    EPSILON=0.0001
    DELTA=0.002
    STEP=0.05

    def __init__(self, mininet, users, nets, demand={}, capacity={}, delay={},
                 start=0, end=600, interval=5):
        self.mn = mininet
        #string list of sta names
        self.users = users
        #string list of lte/ap names
        self.nets = nets
        #dict {net name: backbone net name}
        self.backbones={}
        #string of the single server
        self.server=""
        if(len(mininet.hosts)==1):
            self.server=mininet.hosts[0].name
        else:
            info('more than one hosts in Mininet')
            exit(0)
        #string of hub name
        self.hub=""
        for intf in mininet.nameToNode[self.server].intfList():
            if intf:
                intfs=[intf.link.intf1,intf.link.intf2]
                intfs.remove(intf)
                self.hub=intfs[0].node.name
        info('hub name is:')
        print(self.hub)
        #connectivity is a dict{string link pair: value 1/0}
        self.connectivity ={}

        self.IPTable={}
        #dict{sta-net or net-backbone link: interface name}
        self.linkToIntf={}
        self.prev_flow={}
        #demand is a dictionary{sta name: double}
        self.demand=demand
        #capacity is a dict {string link pair: double}
        #assumption: capacity for sta-lte link and ltebackbone-hub link are set
        self.capacity=capacity
        #delay is a dict {string link pair: double}
        #assumption: delay for sta-lte and ltebackbone-hub links are set
        self.delay=delay
        self.alpha = 0
        self.global_opt=0
        #initialize AP related
        for user in users:
            for net in nets:
                key=user+'-'+net
                self.connectivity[key]=0

        for user in users:
            sta=mininet.nameToNode[user]
            staid=user.lstrip('sta')
            ip='10.0.'+staid+'.0'
            #deal with AP
            for idx, wlan in enumerate( sta.params['wlan']):
                self.IPTable[wlan]=ip
                ip=getNextIP(ip)
                apNet=sta.params['associatedTo'][idx]
                if(apNet):
                    key=user+'-'+apNet.name
                    self.connectivity[key]=1
                    self.linkToIntf[key]=wlan
                    #get dynamic link dist, cap, latency
                    dist=sta.get_distance_to(apNet)
                    cap=wirelessLink.setBW(sta=sta, ap=apNet, dist=dist)
                    #print(key+" capacity is: ",cap)
                    self.capacity[key]=cap
                    latency=wirelessLink.setLatency(dist)
                    #print(key+" latency is: ", latency)
                    self.delay[key]=latency
            #deal with Eth
            for idx, intf in enumerate(sta.intfList()):
                if(intf.name not in self.IPTable):
                    eth=intf.name
                    self.IPTable[eth]=ip
                    ip=getNextIP(ip)
                    intfs=[intf.link.intf1, intf.link.intf2]
                    intfs.remove(intf)
                    lteNet=intfs[0].node
                    key=user+'-'+lteNet.name
                    self.connectivity[key]=1
                    self.linkToIntf[key]=eth

        # Find the backbone of Net
        for net in self.nets:
            Net=mininet.nameToNode[net]
            for intf in Net.intfList():
                if intf.link:
                    intfs = [intf.link.intf1, intf.link.intf2]
                    intfs.remove(intf)
                    if (intfs[0] != 'wireless'):
                        otherEnd = intfs[0].node
                        if (otherEnd in mininet.switches and Net.name not in self.backbones):
                            self.backbones[Net.name] = otherEnd.name
                            key = Net.name + '-' + otherEnd.name
                            self.connectivity[key] = 1
                            self.linkToIntf[key] = intf.name
                            key2 = otherEnd.name + '-' + self.hub
                            self.connectivity[key2] = 1

        key=self.hub+'-'+self.server
        self.connectivity[key]=1

        info("connecitvities:")
        print(self.connectivity)
        info("IP tables:")
        print(self.IPTable)
        info("link and interfaces:")
        print(self.linkToIntf)
        info("backbones")
        print(self.backbones)

        thread=threading.Thread(target=self.checkChange, args=(start, end, interval))
        thread.daemon=True
        thread.start()




    def checkChange(self, start, end, interval):
        threshold=0.3
        for i in range(start,end,interval):
            print("round ",i)
            changeList = set([])
            new_conn = {}
            #initialize new connectivity
            for user in self.users:
                for net in self.nets:
                    key = user + '-' + net
                    new_conn[key] = 0


            for user in self.users:
                sta=self.mn.nameToNode[user]
                for idx, wlan in enumerate(sta.params['wlan']):
                    apNet=sta.params['associatedTo'][idx]
                    if(apNet):
                        key = user + '-' + apNet.name
                        self.linkToIntf[key]=wlan
                        new_conn[key]=1
                        dist = sta.get_distance_to(apNet)
                        cap = wirelessLink.setBW(sta=sta, ap=apNet, dist=dist)
                        latency = wirelessLink.setLatency(dist)

                        if(self.connectivity[key]==0):
                            changeList.add(user)
                            self.capacity[key]=cap
                            self.delay[key]=latency
                        else:
                            cap_change=abs(cap-self.capacity[key])/self.capacity[key]
                            lat_change=abs(latency-self.delay[key])/self.delay[key]
                            if(cap_change>threshold and lat_change>threshold):
                                changeList.add(user)
                                self.capacity[key]=cap
                                self.delay[key]=latency

                        print(key + " new capacity is: ", cap)
                        print(key + " new latency is: ", latency)
            #self.connectivity.clear()
            self.connectivity.update(new_conn)
            print(changeList)
            if(len(changeList)>0):
            #run FDM
                self.run_FDM(changeList)

            time.sleep(interval)

    def run_FDM(self, changeList, quickStart=False, use_alpha=False):
        n_host=len(self.users)+1
        n_net =len(self.nets)
        cnt=0
        user_to_host={}
        net_to_wireless={}
        host_to_user={}
        wireless_to_net={}
        backbone_to_end={}
        end_to_backbone={}

        #Adding nodes
        for user in self.users:
            user_to_host[user]=cnt
            host_to_user[cnt]=user
            cnt+=1
        server=cnt
        cnt+=1
        for net in self.nets:
            net_to_wireless[net]=cnt
            wireless_to_net[cnt]=net
            backbone_to_end[self.backbones[net]]=cnt+n_net
            end_to_backbone[cnt+n_net]=self.backbones[net]
            cnt+=1
        cnt+=n_net

        hub=cnt
        nn=hub+1
        #Naming nodes
        names=range(nn)
        for i in range(nn):
            if(i in host_to_user):
                names[i]=host_to_user[i]
            if(i in wireless_to_net):
                names[i]=wireless_to_net[i]
            if(i in end_to_backbone):
                names[i]=end_to_backbone[i]
            if(i==hub):
                names[i]=self.hub
            if(i==server):
                names[i]=self.server

        info(names)
        #Addling links
        v_pairs=[]
        for i in range(n_host-1):
            for j in range(n_host,n_host+n_net):
                key=host_to_user[i]+'-'+wireless_to_net[j]
                v_pair={}
                if(self.connectivity[key]):
                    v_pair['End1']=i
                    v_pair['End2']=j
                    #if there no key found, assume 0
                    if(key in self.delay):
                        v_pair['delay']=self.delay[key]
                    # if there no key found, assume inf
                    if(key in self.capacity):
                        v_pair['cap']=self.capacity[key]
                    v_pairs.append(v_pair)

        for i in range(n_host,n_host+n_net):
            key=wireless_to_net[i]+'-'+end_to_backbone[i+n_net]
            v_pair={}
            if(key in self.connectivity):
                v_pair['End1']=i
                v_pair['End2']=i+n_net
                if(key in self.delay):
                    v_pair['delay']=self.delay[key]
                if(key in self.capacity):
                    v_pair['cap']=self.capacity[key]
                v_pairs.append(v_pair)
        for i in range(n_host,n_host+n_net):
            key=end_to_backbone[i+n_net]+'-'+names[hub]
            v_pair={}
            if(key in self.connectivity):
                v_pair['End1']=i
                v_pair['End2']=hub
                if(key in self.delay):
                    v_pair['delay']=self.delay[key]
                if(key in self.capacity):
                    v_pair['cap']=self.capacity[key]
                v_pairs.append(v_pair)
        if True:
            key = names[hub] + names[server]
            v_pair={}
            if (key in self.connectivity):
                v_pair['End1'] = hub
                v_pair['End2'] = server
                if (key in self.delay):
                    v_pair['delay'] = self.delay[key]
                if (key in self.capacity):
                    v_pair['cap'] = self.capacity[key]
                v_pairs.append(v_pair)

        nl=len(v_pairs)
        info('got v_pairs\n')
        info(v_pairs)
        Adj=[[] for i in range(nn)]
        End1, End2, Cap, Cost, Offset, Gflow, Eflow, Pflow, FDlen, NewCap=([0 for j in range(nl)] for i in range(10))
        SPdist, SPpred=([[0 for k in range(nn)] for i in range(nn)] for i in range(2))

        for idx, it in enumerate(v_pairs):
            End1.append(it['End1'])
            End2.append(it['End2'])
            Adj[it['End1']].append(idx)
            if('delay' in it):
                Cost[idx]=it['delay']
            else:
                Cost[idx]=0
            if('cap' in it):
                Cap[idx]=it['cap']
            else:
                Cap[idx]=float("inf")
            Offset[idx]=-1
            if(it['End1'] in host_to_user):
                if(host_to_user[it['End1']] not in changeList):
                    key=host_to_user[it['End1']]+'-'+wireless_to_net[it['End2']]
                    if(key in self.prev_flow):
                        Offset[idx]=self.prev_flow[key]

        Gtable, Etable = ([{} for i in range(nl)] for j in range(2))
        MsgLen='1000B'
        PreviousDelay=float("inf")
        Req=[[0 for i in range(nn)]for j in range(nn)]
        MM_Req = [[0 for i in range(nn)] for j in range(nn)]
        TotReq=0
        for i in range(n_host-1):
            Req[i][server]=self.demand[host_to_user[i]]
            TotReq+=Req[i][server]
        return
        ###FDM algorithm

        #Initialize Gflow
        #Normal Start
        if(not quickStart):
            self.SetLinkLens(nl, Gflow, Cap, MsgLen, FDlen, Cost, Offset)
            self.SetSP(nn, End2, FDlen, Adj, SPdist, SPpred)
            self.LoadLinks(nn,nl,Req,SPpred,End1,Gflow,Gtable,names)
        else:
            #Quick start
            #load previous flows for users not in CL
            for user in self.users:
                if(user in changeList):
                    host=user_to_host[user]
                    for l in Adj[host]:
                        key=user+'-'+wireless_to_net[End2[l]]
                        ip=self.IPTable[self.linkToIntf[key]]
                        if self.prev_flow[key]:
                            f=self.prev_flow[key]
                            Gflow[l]+=f
                            Gtable[l][ip]+=f
                            s =End2[l]
                            while(s!=server):
                                l=Adj[s][0]
                                Gflow[l]+=f
                                Gtable[l][ip]+=f
                                s=End2[l]
            self.SetLinkLens(nl, Gflow, Cap, MsgLen, FDlen, Cost, Offset)
            for user in self.users:
                if user in changeList:
                    host=user_to_host[user]
                    self.Bellman(nn,host,End2,FDlen,Adj,SPpred[host],SPdist[host])
                    self.LoadLInksInCL(host,server,Req[host][server],SPpred,End1,Gflow,Gtable,names)
        Aresult=self.AdjustCaps(nl, Gflow, Cap, NewCap)
        if Aresult==1:
            Aflag=0
        else:
            Aflag=1
        CurrentDelay=self.CalcDelay(nl, Gflow, NewCap, MsgLen, TotReq, Cost, Offset)
        ori_delay=self.CalcDelay_ori(nl, Gflow, NewCap, MsgLen, TotReq, Cost)
        info(CurrentDelay, " ", ori_delay)

        delta, rho, step, gamma=[0.2, 0.2, 5, 2]
        req=TotReq/len(self.users)

        if(use_alpha and len(changeList)<len(self.users)):
            self.alpha=rho*max(CurrentDelay-self.global_opt,0.0)/step/(len(self.users)-len(changeList))\
                       /(delta*delta*req*req)
        count=0
        feasible=1
        while(Aflag or (CurrentDelay<PreviousDelay*(1-self.EPSILON))):
            self.SetLinkLens(nl, Gflow, NewCap, MsgLen, FDlen, Cost, Offset)
            self.SP(nn, End2, FDlen, Adj, SPdist, SPpred)
            self.LoadLinks(nn, nl, Req, SPpred, End1, Eflow, Etable, names)
            PreviousDelay=self.CalcDelay(nl, Gflow, NewCap, MsgLen, TotReq, Cost, Offset)
            self.Superpose(nl, Eflow, Gflow, NewCap, TotReq, MsgLen, Cost, Offset, Gtable, Etable)
            CurrentDelay=self.CalcDelay(nl, Gflow, NewCap, MsgLen, TotReq, Cost, Offset)
            tmp_delay=CurrentDelay
            if(Aflag):
                Aresult=self.AdjustCaps(nl, Gflow, Cap, NewCap)
                if Aresult==1:
                    Aflag=0
                else:
                    Aflag=1

            CurrentDelay=self.CalcDelay(nl, Gflow, NewCap, MsgLen, TotReq, Cost, Offset)
            ori_delay = self.CalcDelay_ori(nl, Gflow, NewCap, MsgLen, TotReq, Cost)
            info(CurrentDelay, " ", ori_delay)
            if((Aflag==1 and (CurrentDelay>=PreviousDelay*(1-self.EPSILON))) or count>=100000):
                info('Problem is infeasible')
                feasible=0
                break
            count+=1
        if(feasible):
            '''Printing flow tables and stuff'''
        else:
            '''Max-min reduce request'''








    def SetLinkLens(self, nl, Flow, Cap, MsgLen, Len, Cost, Off):
        return
    def SetSP(self, nn, End2, Len, Adj, SPdist, SPpred):
        return
    def Bellman(self, nn, root, End2, LinkLength, Adj, Pred, Dist):
        return
    def LoadLinks(self, nn, nl, Req, SPpred, End1, Flow, Table, Names):
        return
    def LoadLInksInCL(self,src, dst, req, SPpred, End1, Flow, Table, Names):
        return
    def AdjustCaps(self, nl, Flow, Cap, NewCap):
        return 0
    def CalcDelay(self, nl, Flow, Cap, MsgLen, TotReq, Cost, Off):
        return 0
    def Superpose(self, nl, Eflow, Gflow, Cap, TotReq, MsgLen, Cost, Off, Gtable, Etable):
        return
    def FindX(self, nl, Gflow, Eflow, Cap, TotReq, MsgLen, Cost, Off):
        return 0
    def LinkDelay(self, Flow, Cap, MsgLen, Cost):
        return 0
    def DerivDelay(self, Flow, Cap, MsgLen, Cost, Off):
        return 0
    def DelayF(self, x, nl, Eflow, Gflow, Cap, MsgLen, TotReq, Cost, Off):
        return 0
    def CalcDelay_ori(self, nl, Flow, Cap, MsgLen, TotReq, Cost):
        return 0
































        
