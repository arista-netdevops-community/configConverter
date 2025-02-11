# Copyright (c) 2024 Arista Networks, Inc.
# Use of this source code is governed by the MIT license
# that can be found in the LICENSE file.

from confparser import confparser
import yaml
import argparse
import pyavd.get_device_config
import re
import sys
from copy import deepcopy

parser = argparse.ArgumentParser()
parser.add_argument("-i", required=True, help="input file")
parser.add_argument("--dissector", default="ios.yaml", help="dissector file. default=ios.yaml")
parser.add_argument("--output", default="text", help="default=text, text|yaml")
parser.add_argument("--debug", default=False, action="store_true", help="emit some debug values")

args = parser.parse_args()

notifications = []

def _findPolicyMap(pm, policyMaps):
    if pm == None or "qos" not in policyMaps:
        return None

    ####  this function could be more pythonic, sacrificing that for readability
    for definedPM in policyMaps["qos"]:
        if definedPM["name"] == pm:
            return definedPM

    return None

def setupInterface(interface, oldInterface, policyMaps):
    # this function will take the interface dict passed to it and convert it into
    #  pyavd config
    newInterface = {}
    # there may be features that avd does not yet support and we need to add as
    #  raw cli commands.  store any here
    newInterfaceRawCLI = []
    if interface.startswith("Port") or interface.startswith("Vlan"):
        newInterface["name"] = interface
    else:
        intfName = interface.split('/')
        newInterface["name"] = f"ethernet{intfName[-1]}"

    if "description" in oldInterface:
        newInterface["description"] = oldInterface.pop("description")
        if isinstance(newInterface["description"], list):
            notifications.append(f'We appear to have two copies of {newInterface["name"]} in the source config!')
            return False
    if "vlans" in oldInterface:
        newInterface["vlans"] = oldInterface.pop("vlans")
    if "mode" in oldInterface:
        newInterface["mode"] = oldInterface.pop("mode")
    if "shutdown" in oldInterface:
        newInterface["shutdown"] = oldInterface.pop("shutdown")
    if "mtu" in oldInterface:
        newInterface["mtu"] = oldInterface.pop("mtu")
    if "logging" in oldInterface:
        # i didn't see a way in the yaml to set a complex key structure so we need to do this by hand
        newInterface["logging"] = {}
        if "link-status" in oldInterface["logging"]:
            newInterface["logging"].update({"event": {"link_status": oldInterface["logging"].pop("link-status")}})

        if len(oldInterface["logging"]) == 0:
            oldInterface.pop("logging")

    if "link_trap" in oldInterface:
        newInterface["snmp_trap_link_change"] = oldInterface.pop("link_trap")

    if "storm_control" in oldInterface:
        newInterface["storm_control"] = {}
        if "broadcast" in oldInterface["storm_control"]:
            #FIXME - the pop returns a confparser class. this can't be the best way to cast this?
            newInterface["storm_control"]["broadcast"] = yaml.safe_load(f'{oldInterface["storm_control"].pop("broadcast")}')

        #FIXME storm_control action?
        """    "storm_control": {
                 "action": [
                     "shutdown",
                     "trap"
                 ]
               },"""
        #FIXME
        if "action" in oldInterface["storm_control"]:
            oldInterface["storm_control"].pop("action")

        if len(oldInterface["storm_control"]) == 0:
            oldInterface.pop("storm_control")

    if "lldp" in oldInterface:
        #FIXME - the pop returns a confparser class. this can't be the best way to cast this?
        newInterface["lldp"] = yaml.safe_load(f'{oldInterface.pop("lldp")}')

    # FIXME we need to do something special here
    #  our speed/duplex settings are pretty different than cisco
    #  presently we only look for 10m, 100m, 1000m
    #  the logic here is a bit messy. it's written long here for clarity
    #   ) if speed not set, leave default
    #   ) if duplex is not set, assume auto
    #   ) if duplex and speed set, forced

    if "10" in oldInterface.get("speed", []):
        notifications.append(f"!!!!!!!!!!!!!!! {interface} has 10Meg.  Care must be taken as to the destination switch capabilities!!!!")

    if not "speed" in oldInterface and "duplex" in oldInterface:
        oldInterface.pop("duplex")
    elif "speed" in oldInterface and not "duplex" in oldInterface:
        tmp = []
        for speed in oldInterface["speed"]:
            if speed == "auto":
                tmp.append("auto")
                continue
            if speed == "1":
                tmp.append("1gfull")
                tmp.append("1000full")
            elif speed == "100":
                tmp.append("100full")
                tmp.append("100half")
            elif speed == "10":
                tmp.append("10full")
                tmp.append("10half")
        newInterface["speed"] = " ".join(tmp)
        oldInterface.pop("speed")
    elif "speed" in oldInterface and "duplex" in oldInterface:
        # let's force this!
        newInterface["speed"] = f"{oldInterface['speed'][0]}{oldInterface['duplex']}"
        oldInterface.pop("speed")
        oldInterface.pop("duplex")
        
    if "spanning_tree_guard" in oldInterface:
        newInterface["spanning_tree_guard"] = oldInterface.pop("spanning_tree_guard")

    if "channel_group" in oldInterface:
        #FIXME - the pop returns a confparser class. this can't be the best way to cast this?
        newInterface["channel_group"] = yaml.safe_load(f'{oldInterface.pop("channel_group")}')

    if "native_vlan" in oldInterface:
        newInterface["native_vlan"] = oldInterface.pop("native_vlan")

    if "ipv4" in oldInterface and oldInterface["ipv4"] != False:
        #if "ethernet" in newInterface["name"].lower():
            #newInterface[] = no switchport
        newInterface["ip_address"] = oldInterface.pop("ipv4")
        if isinstance(newInterface["ip_address"], list):
            notifications.append(f'We appear to have two copies of {newInterface["name"]} in the source config!')
            return False

    if "ipv4_secondary" in oldInterface:
        newInterface["ip_address_secondaries"] = []
        if isinstance(oldInterface["ipv4_secondary"], list):
            for sec in oldInterface["ipv4_secondary"]:
                newInterface["ip_address_secondaries"].append(sec)
        else:
            newInterface["ip_address_secondaries"].append(oldInterface["ipv4_secondary"])

        oldInterface.pop("ipv4_secondary")

    if "ipv4_redirects" in oldInterface:
        newInterface["ip_icmp_redirect"] = oldInterface.pop("ipv4_redirects")

    if "ipv4_proxy_arp" in oldInterface:
        newInterface["ip_proxy_arp"] = oldInterface.pop("ipv4_proxy_arp")

    # the 720xp is best handled by using an interface level shaper.
    #  i need to parse
    #    out what service-policy is on an interface
    #    look for that policy-map
    #    look at the first class in that policy-map
    #    find the right rate and unit
    #    convert it to kbps
    #    emit the shaper value

    if "service_policy" in oldInterface:
        sp = oldInterface.pop("service_policy")
        if egressPolicy := sp.get("egress", None):
            pm = _findPolicyMap(egressPolicy, policyMaps)
            if pm:
                if "police" in pm["classes"][0]:
                    rate = int(pm["classes"][0]["police"]["rate"])
                    unit = pm["classes"][0]["police"]["rate_unit"]
                    if unit == "bps":
                        rate = int(rate/1024)
                        unit = "kbps"
                    elif unit == "mbps":
                        rate = int(rate*1024)
                        unit = "kbps"
                    elif unit == "pps":
                        unit = "pps"

                    newInterface["shape"] = { "rate": f'{rate} {unit}'}

        if ingressPolicy := sp.get("ingress", sp.get("egress", None)):
            pm = _findPolicyMap(ingressPolicy, policyMaps)
            if pm:
                newInterface["service_policy"] = {"qos": {"input": pm["name"]}}

    if "standby" in oldInterface:
        newInterface["vrrp_ids"] = []
        vrrp = deepcopy(oldInterface["standby"])
        oldInterface["standby"].pop("version")
        for vrid, vridData in vrrp.items():
            if vrid.isdigit():
                #ipv4 at least is pretty important
                if "ip" not in vridData:
                    continue

                #  this is a new vrid.  convert it
                newVRID = {
                    "id": int(vrid),
                    "ipv4": {
                        "address": vridData["ip"],
                        "version": 2
                    }
                }
                if "priority" in vridData:
                    newVRID["priority_level"] =  int(vridData["priority"])
                if "preempt" in vridData:
                    newVRID["preempt"] = { "enabled": True}
                    if "delay" in vridData:
                        newVRID["preempt"]["delay"] = { "minimum": int(vridData["delay"]) }
                    if "reload" in vridData:
                        newVRID["preempt"]["reload"] = { "minimum": int(vridData["delay"]) }
                if "vrid_name" in vridData:
                    newInterfaceRawCLI.append(f'vrrp {vrid} session description {vridData["vrid_name"]}')
                if authString := vridData.get("auth_md5", vridData.get("auth", None)):
                    newInterfaceRawCLI.append(f'vrrp {vrid} peer authentication {authString}')

                newInterface["vrrp_ids"].append(newVRID)
                oldInterface["standby"].pop(vrid)

        if not oldInterface["standby"]:
            oldInterface.pop("standby")


    ##### these are things i don't care about
    if "cdp_enable" in oldInterface:
        oldInterface.pop("cdp_enable")
    if "dtp" in oldInterface:
        oldInterface.pop("dtp")
    if "encap" in oldInterface:
        oldInterface.pop("encap")
    if "switchport" in oldInterface:
        oldInterface.pop("switchport")


    # what's left?
    #print(yaml.dump(newInterface, sort_keys=False))
    if len(oldInterface) > 0:
        print(f"****** {newInterface['name']}", file=sys.stderr)
        print(oldInterface, file=sys.stderr)
        print("******", file=sys.stderr)

    if newInterfaceRawCLI:
        newInterface["eos_cli"] = "\n".join(newInterfaceRawCLI)
    return newInterface

def setClassMaps(classMapName, classMap):
    newClassMap = {"name": classMapName}
    if classMap.get("lines", "").find("any") >= 0:
        newClassMap["ip"] = { "access_group": "matchAllv4" }
        newClassMap["ipv6" ] = { "access_group": "matchAllv6" }

    return newClassMap

def setPolicyMaps(policyMapName, policyMap):
    newPolicyMap = { "name": policyMapName }
    newPolicyMap["classes"] = []
    for className, classEntry in policyMap["class"].items():
        newClass = { "name": className }
        if "police" in classEntry:
            newClass["police"] = {}
            if "rate" in classEntry["police"]:
                newClass["police"]["rate"] = classEntry["police"].pop("rate")
            if "rate_unit" in classEntry["police"]:
                newClass["police"]["rate_unit"] = classEntry["police"].pop("rate_unit")
            if "rate_burst_size" in classEntry["police"]:
                newClass["police"]["rate_burst_size"] = classEntry["police"].pop("rate_burst_size")
            if "rate_burst_size_unit" in classEntry["police"]:
                rbu = classEntry["police"].pop("rate_burst_size_unit")
                if rbu == "mbyte":
                    rbu = "mbytes"

                newClass["police"]["rate_burst_size_unit"] = rbu
            if classEntry["police"].get("exceed_action", "") == "drop":
                newClass["police"]["action"] = { "type": "drop-precedence" }
                try:
                    newClass["police"].pop("exceed_action")
                except:
                    pass
        newPolicyMap["classes"].append(deepcopy(newClass))

    #if len(policyMap) > 0:
        #print("POLICYMAP")
        #print(policyMap)
        #print("*******")

    return newPolicyMap

dissector = confparser.Dissector.from_file(args.dissector)
dev = dissector.parse_file(args.i)

if args.debug:
    print(dev)

newDevice = {
        "ethernet_interfaces": [],
        "vlan_interfaces": [],
        "port_channel_interfaces": [],
        "class_maps": {},
        "policy_maps": {},
}

if "class_map" in dev:
    matchAny = False
    newDevice["class_maps"]["qos"] = []
    for classMapName, classMap in dev["class_map"].items():
        matchAny = True
        newDevice["class_maps"]["qos"].append(setClassMaps(classMapName, classMap))
    dev.pop("class_map")

    # we are going to make a huge assumption here and create the match any acls if we had
    #  any class-map in the config

    if matchAny:
        newDevice["ip_access_lists"] = [
                {
                    "name": "matchAllv4",
                    "entries": [
                        {
                            "sequence": 10,
                            "action": "permit",
                            "protocol": "ip",
                            "source": "any",
                            "destination": "any"
                        }
                    ]
                }
        ]
        newDevice["ipv6_access_lists"] = [
                {
                    "name": "matchAllv6",
                    "sequence_numbers": [
                        {
                            "sequence": 10,
                            "action": "permit ipv6 any any",
                        }
                    ]
                }
        ]


    if "policy_map" in dev:
        newDevice["policy_maps"]["qos"] = []
        for policyMapName, policyMap in dev["policy_map"].items():
            newDevice["policy_maps"]["qos"].append(setPolicyMaps(policyMapName, policyMap))
        dev.pop("policy_map")

for interface, interfaceConfig in dev["interface"].items():
    if interface.startswith("Gigabit"):
        if newInterface := setupInterface(interface, interfaceConfig, newDevice["policy_maps"]):
            newDevice["ethernet_interfaces"].append(newInterface)
    elif interface.startswith("TenGiga"):
        newInterfaceName = "InvalidInterface1/10000"
        notifications.append(f"!!!!!!!!!!!!!!! added an invalid interface: {interface} -> {newInterfaceName}")
        if newInterface := setupInterface(newInterfaceName, interfaceConfig, newDevice["policy_maps"]):
            newDevice["ethernet_interfaces"].append(newInterface)
    elif interface.startswith("Vlan"):
        if newInterface := setupInterface(interface, interfaceConfig, newDevice["policy_maps"]):
            newDevice["vlan_interfaces"].append(newInterface)
    elif interface.startswith("Port"):
        if newInterface := setupInterface(interface, interfaceConfig, newDevice["policy_maps"]):
            newDevice["port_channel_interfaces"].append(newInterface)


if args.output == "text":
    print(pyavd.get_device_config(newDevice))
elif args.output == "yaml":
    print(yaml.dump(newDevice))

if confparser.configNonMatchingLines:
    print("did not match any regex on the following lines", file=sys.stderr)
    for nonMatch in confparser.configNonMatchingLines:
        print(nonMatch, file=sys.stderr)
if notifications:
    print("the following notifications where issued by the converter", file=sys.stderr)
    for notification in notifications:
        print(notification, file=sys.stderr)

