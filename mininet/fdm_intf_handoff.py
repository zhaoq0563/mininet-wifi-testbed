from mininet.log import info, output
from mininet.wifiLink import wirelessLink
from subprocess import call, check_call, check_output

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
                 start=0, end=600, interval=5, use_fdm=True, protocol='FDM'):
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
            info('more than one hosts in Mininet\n')
            exit(0)
        #string of hub name
        self.hub=""
        for intf in mininet.nameToNode[self.server].intfList():
            if intf.link:
                intfs=[intf.link.intf1,intf.link.intf2]
                intfs.remove(intf)
                if(intfs[0]!='wireless'):
                    if(intfs[0].node not in mininet.switches or self.hub!=""):
                        info('hub violation\n')
                        #print(intfs[0].node.name, mininet.switches, self.hub)
                        exit(0)
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
        self.use_fdm=use_fdm
        self.protocol=protocol
        self.end=end
        #initialize AP related
        for user in users:
            for net in nets:
                key=user+'-'+net
                self.connectivity[key]=0

        for user in users:
            sta=mininet.nameToNode[user]
            staid=user.lstrip('sta')
            ip='10.0.'+str(int(staid)+1)+'.0'
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
                        if (otherEnd in mininet.switches):
                            if(Net.name in self.backbones):
                                info("net ",Net.name," connected to multiple backbones\n")
                                exit(0)
                            self.backbones[Net.name] = otherEnd.name
                            key = Net.name + '-' + otherEnd.name
                            self.connectivity[key] = 1
                            self.linkToIntf[key] = (intf.name,intfs[0].name)
                            #validate backbone node
                            for b_intf in otherEnd.intfList():
                                if(b_intf.name!='lo'):
                                    b_intfs=[b_intf.link.intf1, b_intf.link.intf2]
                                    b_intfs.remove(b_intf)
                                    tmpEnd=b_intfs[0].node
                                    if tmpEnd in mininet.switches or tmpEnd in mininet.aps:
                                        if( tmpEnd.name not in self.nets and tmpEnd.name!= self.hub):
                                            info('backbone node ',otherEnd.name,' connected to unknown node\n')
                                            exit(0)
                                        if(tmpEnd.name not in self.nets):
                                            key2 = otherEnd.name + '-' + self.hub
                                            self.connectivity[key2] = 1
                                            self.linkToIntf[key2]=(b_intf.name,b_intfs[0].name)

        for intf in mininet.nameToNode[self.hub].intfList():
            if(intf.name!='lo'):
                intfs=[intf.link.intf1, intf.link.intf2]
                intfs.remove(intf)
                if(intfs[0].node.name==self.server):
                    key=self.hub+'-'+self.server
                    self.connectivity[key]=1
                    self.linkToIntf[key]=(intf.name,intfs[0].name)
        '''check keys of demand, capacity and delay'''
        for key in demand:
            if key not in self.users:
                info('demand does not match user\n')
                exit(0)
        for key in capacity:
            if key not in self.connectivity:
                info('capacity key does not match any link in connectivity\n')
                exit(0)
        for key in delay:
            if key not in self.connectivity:
                info('delay key does not match any link in connectivity\n')
                exit(0)

        info("connecitvities:\n")
        print(self.connectivity)
        info("IP tables:\n")
        print(self.IPTable)
        info("link and interfaces:\n")
        print(self.linkToIntf)
        info("backbones\n")
        print(self.backbones)

        thread=threading.Thread(target=self.checkChange, args=(start, end, interval))
        thread.daemon=True
        thread.start()




    def checkChange(self, start, end, interval):
        threshold=0.3
        print("init round")
        self.run_FDM(self.users,True)
        time.sleep(interval)

        for i in range(start,end,interval):
            print("round ",i)
            changeList = set([])
            new_conn = {}
            #initialize new connectivity
            for user in self.users:
                for net in self.nets:
                    if(self.mn.nameToNode[net] not in self.mn.switches):
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

            '''check sptcp user wlan disconnection'''
            for user in self.users:
                old=[]
                new=[]
                for net in self.nets:
                    if(self.mn.nameToNode[net] not in self.mn.switches):
                        key=user+'-'+net
                        old.append(self.connectivity[key])
                        new.append(new_conn[key])
                if old!=new:
                    changeList.add(user)

            #self.connectivity.clear()
            self.connectivity.update(new_conn)
            print(changeList)
            if(len(changeList)>0):
            #run FDM
                self.run_FDM(self.users)

            time.sleep(interval)

    def run_FDM(self, changeList, backboneFT=False, quickStart=False, use_alpha=False):
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

        info(names,'\n\n')
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
                v_pair['End1']=i+n_net
                v_pair['End2']=hub
                if(key in self.delay):
                    v_pair['delay']=self.delay[key]
                if(key in self.capacity):
                    v_pair['cap']=self.capacity[key]
                v_pairs.append(v_pair)
        if True:
            key = names[hub] +'-'+ names[server]
            #info('hub and server: ',key,'\n','\n\n')
            v_pair={}
            if (key in self.connectivity):
                #info('add hub server key\n\n')
                v_pair['End1'] = hub
                v_pair['End2'] = server
                if (key in self.delay):
                    v_pair['delay'] = self.delay[key]
                if (key in self.capacity):
                    v_pair['cap'] = self.capacity[key]
                v_pairs.append(v_pair)

        nl=len(v_pairs)
        #info('got v_pairs\n')
        #info(v_pairs,'\n')
        #info(self.connectivity,'\n')
        Adj=[[] for i in range(nn)]
        End1, End2 =([0 for j in range(nl)] for i in range(2))
        Cap, Cost, Offset, Gflow, Eflow, Pflow, FDlen, NewCap=([float(0) for j in range(nl)] for i in range(8))

        SPdist = [[float(0) for k in range(nn)]for i in range(nn)]
        SPpred = [[0 for k in range(nn)]for i in range(nn)]

        for idx, it in enumerate(v_pairs):
            End1[idx]=it['End1']
            End2[idx]=it['End2']
            Adj[it['End1']].append(idx)
            if('delay' in it):
                Cost[idx]=float(it['delay'])
            else:
                Cost[idx]=float(0)
            if('cap' in it):
                Cap[idx]=float(it['cap'])
            else:
                Cap[idx]=float("inf")
            Offset[idx]=-1
            if(it['End1'] in host_to_user):
                if(host_to_user[it['End1']] not in changeList):
                    key=host_to_user[it['End1']]+'-'+wireless_to_net[it['End2']]
                    if(key in self.prev_flow):
                        Offset[idx]=float(self.prev_flow[key])
        #info("Adj is: \n\n")
        #info(Adj,'\n\n')
        Gtable, Etable = ([{} for i in range(nl)] for j in range(2))
        MsgLen='1000B'
        PreviousDelay=float("inf")
        Req=[[float(0) for i in range(nn)]for j in range(nn)]
        MM_Req = [[float(0) for i in range(nn)] for j in range(nn)]
        TotReq=0
        for i in range(n_host-1):
            Req[i][server]=float(self.demand[host_to_user[i]])
            TotReq+=Req[i][server]

        ###FDM algorithm

        #Initialize Gflow
        #Normal Start
        if(not quickStart):
            self.SetLinkLens(nl, Gflow, Cap, MsgLen, FDlen, Cost, Offset)
            self.SetSP(nn, End2, FDlen, Adj, SPdist, SPpred)
            #info("Predecessors:\n")
            #info(SPpred,'\n\n')
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
        info(CurrentDelay, " ", ori_delay,'\n')

        delta, rho, step, gamma=[0.2, 0.2, 5, 2]
        req=TotReq/len(self.users)

        if(use_alpha and len(changeList)<len(self.users)):
            self.alpha=rho*max(CurrentDelay-self.global_opt,0.0)/step/(len(self.users)-len(changeList))\
                       /(delta*delta*req*req)
        count=0
        feasible=1
        while(Aflag or (CurrentDelay<PreviousDelay*(1-self.EPSILON))):
            self.SetLinkLens(nl, Gflow, NewCap, MsgLen, FDlen, Cost, Offset)
            self.SetSP(nn, End2, FDlen, Adj, SPdist, SPpred)
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
            info(CurrentDelay, " ", ori_delay,'\n')
            CurrentDelay=tmp_delay
            if((Aflag==1 and (CurrentDelay>=PreviousDelay*(1-self.EPSILON))) or count>=100000):
                info('Problem is infeasible\n')
                feasible=0
                break
            count+=1
            info(count,'\n')
        if(feasible):
            '''Printing flow tables and stuff'''

            print(Gtable)
            info('FDM success\n')

            '''Generate Flowtable and queues'''
            ft_book=[{} for i in range(nn)]
            for i in range(nl):
                end1=End1[i]
                end2=End2[i]
                key=names[end1]+'-'+names[end2]
                if end1 not in host_to_user:
                    if 'out' not in ft_book[end1]:
                        ft_book[end1]['out']=[]
                    if 'in' not in ft_book[end2]:
                        ft_book[end2]['in']=[]
                    ft_book[end1]['out'].append(self.linkToIntf[key][0])
                    ft_book[end2]['in'].append(self.linkToIntf[key][1])

            backFTConfig=[]

            if backboneFT:
                backFTConfig= open("flowTable/backFTConfig.sh", "w")
                backFTConfig.write("#!/bin/bash\n\n")

            cnt=1
            for n in range(nn):
                if n not in wireless_to_net and n not in host_to_user and n !=server and backboneFT:
                    '''generate static backhaul flow table'''
                    if(len(ft_book[n]['out'])>1):
                        info('backhaul node ',names[n],' output port number larger than 2\n')
                        print(ft_book[n]['out'])
                        exit(0)
                    outport=ft_book[n]['out'][0]
                    sw,intf1=outport.split('-')
                    intf1_num=intf1[3:]
                    for port in ft_book[n]['in']:
                        sw,intf2=port.split('-')
                        intf2_num=intf2[3:]
                        command="sudo ovs-ofctl add-flow "+names[n]+" in_port="+intf2_num+\
                            ",actions=output:"+intf1_num
                        backFTConfig.write(command+'\n')
                    command="sudo ovs-ofctl add-flow "+names[n]+" in_port="+intf1_num+\
                        ",actions=normal"
                    backFTConfig.write(command+'\n')
                    command="sudo ovs-ofctl add-flow "+names[n]+" priority=100,actions=normal\n"
                    backFTConfig.write(command)
                elif n in wireless_to_net:
                    '''generate dynamic net flow and queue tables'''
                    netFTConfig = open("flowTable/netFTConfig_"+names[n]+".sh", "w")
                    netQConfig = open("flowTable/netQConfig_"+names[n]+".sh", "w")
                    netFTConfig.write("#!/bin/bash\n\n")
                    netQConfig.write("#!/bin/bash\n\n")
                    outport=ft_book[n]['out'][0]
                    sw,intf=outport.split('-')
                    intf_num=intf[3:]
                    link=Adj[n][0]
                    Qcommand="sudo ovs-vsctl -- set Port "+outport+" qos=@newqos -- --id=@newqos"+\
                        " create QoS type=linux-htb other-config:max-rate=100000000 "

                    for idx in range(len(Gtable[link])):
                        Qcommand+=" queues:"+str(idx+cnt)+"=@q"+str(idx+cnt)+" "
                    Qcommand+='-- '
                    for idx,k in enumerate(Gtable[link]):
                        if(self.protocol=='FDM' or self.use_fdm):
                            Qcommand+="--id=@q"+str(idx+cnt)+" create Queue other-config:min-rate="+\
                                str(int(float(Gtable[link][k])*0.9*(10**6)))+" other-config:max-rate="+\
                                str(int(float(Gtable[link][k])*1.2*(10**6)))+" -- "
                            FTcommand = "sudo ovs-ofctl add-flow " + names[n] + " ip,nw_src=" + k + "/32,actions=set_queue:" + \
                                        str(idx + cnt) + ",output:" + intf_num
                        else:
                            userid=int(k.split('.')[2])-1
                            user='sta'+str(userid)
                            Qcommand += "--id=@q" + str(idx + cnt) + " create Queue other-config:min-rate=" + \
                                        str(int(float(self.demand[user]) * (10 ** 6))) + " other-config:max-rate=" + \
                                        str(int(float(self.demand[user]) * (10 ** 6))) + " -- "
                            FTcommand = "sudo ovs-ofctl add-flow " + names[n] + " ip,nw_src=" + k + "/32,actions=output:"+ intf_num
                        netFTConfig.write(FTcommand + '\n')
                    Qcommand=Qcommand.rstrip('-- ')+'\n'
                    netQConfig.write(Qcommand)
                    Qcommand="sudo ovs-ofctl -O Openflow13 queue-stats "+names[n]+'\n'
                    netQConfig.write(Qcommand)
                    FTcommand="sudo ovs-ofctl add-flow "+names[n]+" in_port="+intf_num+",actions=normal\n"
                    netFTConfig.write(FTcommand)
                    FTcommand="sudo ovs-ofctl add-flow "+names[n]+" priority=100,actions=normal\n"
                    netFTConfig.write(FTcommand)
                    cnt+=len(Gtable[link])
                    netFTConfig.close()
                    call(["sudo", "chmod", "777", "flowTable/netFTConfig_"+names[n]+".sh"])
                    netQConfig.close()
                    call(["sudo", "chmod", "777", "flowTable/netQConfig_"+names[n]+".sh"])

            if backboneFT:
                backFTConfig.close()
                call(["sudo","chmod","777","flowTable/backFTConfig.sh"])
                call(["sudo","bash","flowTable/backFTConfig.sh"])
            '''remove tables and queues and apply new ones'''
            info('*** remove net qos, queue, and flow tables ***\n')
            for net in self.nets:
                Net=self.mn.nameToNode[net]
                Net.cmdPrint('sudo ovs-vsctl clear port %s qos'% ft_book[net_to_wireless[net]]['out'][0])
            #print('weird, who is Net???')
            Net.cmdPrint('sudo ovs-vsctl --all destroy qos')
            Net.cmdPrint('sudo ovs-vsctl --all destroy queue')
            for net in self.nets:
                Net = self.mn.nameToNode[net]
                Net.cmdPrint('sudo ovs-ofctl del-flows %s' % net)
                call(["sudo","bash","flowTable/netFTConfig_"+net+".sh"])
                call(["sudo", "bash", "flowTable/netQConfig_" + net + ".sh"])

            '''for MPTCP, if user only has lte connection, then shut off wlan'''
            if(self.protocol=='MPTCP'or self.protocol=='FDM'):
                for user in changeList:
                    used_intf=[]
                    used_lte=[]
                    used_wifi=[]
                    sta=self.mn.nameToNode[user]
                    for net in self.nets:
                        Net=self.mn.nameToNode[net]
                        key=user+'-'+net
                        if(self.connectivity[key]):
                            used_intf.append(self.linkToIntf[key])
                            if Net in self.mn.switches:
                                used_lte.append(self.linkToIntf[key])
                            else:
                                used_wifi.append(self.linkToIntf[key])
                            sta.cmdPrint("ip link set dev "+self.linkToIntf[key]+" multipath on")
                    print("user " +user+" is using following interfaces ", used_intf)
                    for intf in sta.intfList():
                        if intf.name!='lo' and intf.name not in used_intf:
                            sta.cmdPrint("ip link set dev "+intf.name+" multipath off")
                            print("interface "+intf.name+" is not used")
                    if len(used_wifi)>0:
                        intf=used_wifi[0]
                        print("default interface: ",intf)
                        sta.cmdPrint("ip route change default scope global nexthop via "+self.IPTable[intf] +" dev "+  intf)
                    elif len(used_lte)>0:
                        intf=used_lte[0]
                        print("default interface: ", intf)
                        sta.cmdPrint("ip route change default scope global nexthop via "+self.IPTable[intf] +" dev "+  intf)
                    else:
                        info("user ",user, "has no network\n")
                        exit(0)
            else:
                '''If SPTCP, prefer wifi interface, otherwise lte'''
                for user in changeList:
                    used_intf=[]
                    used_lte=[]
                    used_wifi=[]
                    sta=self.mn.nameToNode[user]
                    for net in self.nets:
                        Net=self.mn.nameToNode[net]
                        key=user+'-'+net
                        if(self.connectivity[key]):
                            used_intf.append(self.linkToIntf[key])
                            if Net in self.mn.switches:
                                used_lte.append(self.linkToIntf[key])
                            else:
                                used_wifi.append(self.linkToIntf[key])
                            #sta.cmdPrint("ip link set dev "+self.linkToIntf[key]+" multipath on")
                    print("\nuser " +user+" is using following interfaces ", used_intf)
                    if len(used_wifi)>0:
                        intf=used_wifi[0]
                        print("default interface: ",intf)
                        sta.cmdPrint("ip route change default scope global nexthop via "+self.IPTable[intf] +" dev "+  intf)
                        #sta.cmdPrint("kill $PID")
                        #self.ITGTest(sta,self.mn.nameToNode[self.server], self.demand[user],self.end)
                    elif len(used_lte)>0:
                        intf=used_lte[0]
                        print("default interface: ", intf)
                        sta.cmdPrint("ip route change default scope global nexthop via "+self.IPTable[intf] +" dev "+  intf)
                        #sta.cmdPrint("kill $PID")
                        #self.ITGTest(sta,self.mn.nameToNode[self.server], self.demand[user],self.end)
                    else:
                        info("user ",user, "has no network\n")
                        exit(0)

        else:
            '''Max-min reduce request'''
            info('FDM success infeasible\n')


    def ITGTest(client, server, bw, sTime):
        info('Sending message from ', client.name, '<->', server.name, '\n')
        client.cmd('pushd ~/D-ITG-2.8.1-r1023/bin')
        client.cmd('./ITGSend -T TCP -a 10.0.0.1 -c 1000 -O ' + str(bw) + ' -t ' + str(sTime) + ' -l log/' + str(
            client.name) + '.log -x log/' + str(client.name) + '-' + str(server.name) + '.log &')
        client.cmdPrint('PID=$!')
        client.cmd('popd')


    def SetLinkLens(self, nl, Flow, Cap, MsgLen, Len, Cost, Off):
        for l in range(nl):
            Len[l]=self.DerivDelay(Flow[l],Cap[l],MsgLen,Cost[l],Off[l])
        return

    def SetSP(self, nn, End2, Len, Adj, SPdist, SPpred):
        for node in range(nn):
            self.Bellman(nn,node,End2,Len,Adj,SPpred[node],SPdist[node])
        return

    def Bellman(self, nn, root, End2, LinkLength, Adj, Pred, Dist):
        #info("initial pred:\n")
        #info(Pred,'\n')
        hop=[0 for i in range(nn)]
        for i in range(nn):
            Dist[i]=float("inf")
        Dist[root]=0
        Pred[root]=root

        stack=[root]
        #info("root is: ",root,'\n')
        while(len(stack)):
            node=stack.pop()
            #info(node,'\n')
            #info("adj of node is: ",Adj[node],'\n')
            for i in range(len(Adj[node])):
                curlink=Adj[node][i]
                #info("curlink is: ",curlink,'\n')
                node2=End2[curlink]
                #info("node2 is: ",node2,'\n')
                d=Dist[node]+LinkLength[curlink]
                if(Dist[node2]>d):
                    Dist[node2]=d
                    Pred[node2]=curlink
                    hop[node2]=hop[node]+1
                    #if(hop[node2]<xxx
                    stack.append(node2)
        #info("final pred:\n")
        #info(Pred,'\n')
        return

    def LoadLinks(self, nn, nl, Req, SPpred, End1, Flow, Table, Names):
        for i in range(nl):
            Flow[i]=0
            Table[i].clear()
        for s in range(nn):
            for d in range(nn):
                if(Req[s][d]>0):
                    m=d
                    path_node=[m]
                    path_link=[]
                    while(m!=s):
                        link=SPpred[s][m]
                        p=End1[link]
                        path_node.append(p)
                        path_link.append(link)
                        m=p

                    key=Names[s]+'-'+Names[path_node[-2]]
                    #print(path_node,"key is: ",key)
                    ip=self.IPTable[self.linkToIntf[key]]
                    for k in range(len(path_link)):
                        Flow[path_link[k]]+=Req[s][d]
                        Table[path_link[k]][ip]=Req[s][d]
        return

    def LoadLInksInCL(self,src, dst, req, SPpred, End1, Flow, Table, Names):
        m=dst
        path_node=[dst]
        path_link=[]
        while m != src:
            link=SPpred[src][m]
            p=End1[link]
            path_node.append(p)
            path_link.append(link)
            m=p
        key=Names[src]+'-'+Names[path_node[-2]]
        ip=self.IPTable[self.linkToIntf[key]]
        for k in range(len(path_link)):
            Flow[path_link[k]]+= req
            Table[path_link[k]][ip]+=req
        return

    def AdjustCaps(self, nl, Flow, Cap, NewCap):
        factor=1
        for i in range(nl):
            factor=max(factor,(1+self.DELTA)*Flow[i]/Cap[i])
        for i in range(nl):
            NewCap[i]=factor*Cap[i]
        return factor

    def CalcDelay(self, nl, Flow, Cap, MsgLen, TotReq, Cost, Off):
        sum, norm=[0.0,0.0]
        for i in range(nl):
            sum+=Flow[i]*self.LinkDelay(Flow[i],Cap[i],MsgLen,Cost[i])
            if(Off[i]>=0):
                norm+=self.alpha*(Flow[i]-Off[i])*(Flow[i]-Off[i])
        return sum/TotReq+norm

    def Superpose(self, nl, Eflow, Gflow, Cap, TotReq, MsgLen, Cost, Off, Gtable, Etable):
        x=self.FindX(nl,Gflow,Eflow,Cap,TotReq,MsgLen,Cost,Off)
        for l in range(nl):
            Gflow[l]=x*Eflow[l]+(1-x)*Gflow[l]
            for it in Gtable[l]:
                Gtable[l][it]*=(1-x)
            for it in Etable[l]:
                if it not in Gtable[l]:
                    Gtable[l][it]=Etable[l][it]*x
                else:
                    Gtable[l][it]+=Etable[l][it]*x
        return

    def FindX(self, nl, Gflow, Eflow, Cap, TotReq, MsgLen, Cost, Off):
        st,end=[0.0,1.0]
        xlimit=float(0)
        Flow=[float(0) for i in range(nl)]
        while (end-st)>0.0001:
            exc=False
            xlimit=(st+end)/2
            for i in range(nl):
                Flow[i]=xlimit*Eflow[i]+(1-xlimit)*Gflow[i]
                if(Flow[i]>Cap[i]):
                    exc=True
                    break
            if(exc):
                end=xlimit
            else:
                st=xlimit
        xlimit=st

        x0=0.0
        f0=self.DelayF(x0,nl,Eflow,Gflow,Cap,MsgLen,TotReq,Cost,Off)
        x4=xlimit
        f4=self.DelayF(x4,nl,Eflow,Gflow,Cap,MsgLen,TotReq,Cost,Off)
        x2=(x0+x4)/2
        f2=self.DelayF(x2,nl,Eflow,Gflow,Cap,MsgLen,TotReq,Cost,Off)

        while((x4-x0)>self.EPSILON):
            x1=(x0+x2)/2
            f1 = self.DelayF(x1, nl, Eflow, Gflow, Cap, MsgLen, TotReq, Cost, Off)
            x3 = (x2 + x4) / 2
            f3 = self.DelayF(x3, nl, Eflow, Gflow, Cap, MsgLen, TotReq, Cost, Off)
            if((f0<=f1)or(f1<=f2)):
                x4=x2
                x2=x1
                f4=f2
                f2=f1
            elif f2<=f3:
                x0=x1
                x4=x3
                f0=f1
                f4=f3
            else:
                x0=x2
                x2=x3
                f0=f2
                f2=f3
        if((f0<=f2)and(f0<=f4)):
            return x0
        elif f2<=f4:
            return x2
        else:
            return f4
    def convertMsgLen(self,MsgLen):
        pos=0
        for idx,char in enumerate(MsgLen):
            if char.isalpha():
                pos=idx
                break

        unit=MsgLen[pos:]
        value=float(MsgLen[:pos])
        if unit=='b':
            return value
        elif unit=='B':
            return value*8
        elif unit=='Kb':
            return value*1000
        elif unit=='KB':
            return value*8000
        elif unit=='Mb':
            return value*(10**6)
        elif unit=='MB':
            return value*8*(10**6)
        else:
            info('MsgLen unit error')
            exit(0)

    def LinkDelay(self, Flow, Cap, MsgLen, Cost):
        return self.convertMsgLen(MsgLen)/(Cap*10**6)/(1-Flow/Cap)+float(Cost)/1000

    def DerivDelay(self, Flow, Cap, MsgLen, Cost, Off):
        f=1-Flow/Cap
        norm=float(0)
        if(Off>=0):
            norm=self.alpha*2*(Flow-Off)
        return (self.convertMsgLen(MsgLen)/(Cap*10**6))/(f*f)+float(Cost)/1000+norm

    def DelayF(self, x, nl, Eflow, Gflow, Cap, MsgLen, TotReq, Cost, Off):
        Flow=[float(0) for i in range(nl)]
        for l in range(nl):
            Flow[l]=x*Eflow[l]+(1-x)*Gflow[l]
        return self.CalcDelay(nl,Flow,Cap,MsgLen,TotReq,Cost,Off)

    def CalcDelay_ori(self, nl, Flow, Cap, MsgLen, TotReq, Cost):
        sum=0.0
        for u in range(nl):
            sum+=Flow[u]*self.LinkDelay(Flow[u],Cap[u],MsgLen,Cost[u])
        return sum/TotReq
































        
