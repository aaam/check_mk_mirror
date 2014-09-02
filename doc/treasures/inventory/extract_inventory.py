#!/usr/bin/python
# -*- encoding: utf-8; py-indent-offset: 4 -*-
# +------------------------------------------------------------------+
# |             ____ _               _        __  __ _  __           |
# |            / ___| |__   ___  ___| | __   |  \/  | |/ /           |
# |           | |   | '_ \ / _ \/ __| |/ /   | |\/| | ' /            |
# |           | |___| | | |  __/ (__|   <    | |  | | . \            |
# |            \____|_| |_|\___|\___|_|\_\___|_|  |_|_|\_\           |
# |                                                                  |
# | Copyright Mathias Kettner 2014             mk@mathias-kettner.de |
# +------------------------------------------------------------------+
#
# This file is part of Check_MK.
# The official homepage is at http://mathias-kettner.de/check_mk.
#
# check_mk is free software;  you can redistribute it and/or modify it
# under the  terms of the  GNU General Public License  as published by
# the Free Software Foundation in version 2.  check_mk is  distributed
# in the hope that it will be useful, but WITHOUT ANY WARRANTY;  with-
# out even the implied warranty of  MERCHANTABILITY  or  FITNESS FOR A
# PARTICULAR PURPOSE. See the  GNU General Public License for more de-
# ails.  You should have  received  a copy of the  GNU  General Public
# License along with GNU Make; see the file  COPYING.  If  not,  write
# to the Free Software Foundation, Inc., 51 Franklin St,  Fifth Floor,
# Boston, MA 02110-1301 USA.

# Author: Goetz Golla, gg@mathias-kettner.de

# This script extracts data of the hardware inventory to csv files


relations = {
    "devices": {
        "columns": (
            ( "@hostname", "device_key" ),
            ( "hardware.system.manufacturer", "device_manufacturer" ),
            ( "hardware.system.family", "device_model" ),
            ( "hardware.system.serial", "serial_number" ),
            ( "software.os.name", "operating_system" ),
            ( "@inventory_date", "inventory_date" ),
            ( "software.os.install_date", "installation_date" ),
            ( "hardware.cpu.sockets", "cpu_socket_count" ),
            ( "hardware.cpu.cpus", "cpu_chip_count" ),
            ( "hardware.cpu.cores", "cpu_core_count" ),
            ( "hardware.cpu.max_speed", "cpu_speed" ),
            ( "hardware.cpu.model",  "cpu_name" ),
            ),
        "filter": {},
        "converter": {
            "software.os.install_date": lambda val: time.strftime("%Y-%m-%d", time.localtime(val)),
            "@inventory_date": lambda val: time.strftime("%Y-%m-%d", time.localtime(val)),
            "hardware.cpu.max_speed": lambda val: val/1000000, # hz in mhz
            },
    },
    "inv_raw_arp": {
        "columns": (
            ( "software.packages:*.vendor", "publisher" ),
            ( "software.packages:*.summary", "product" ),
            ( "software.packages:*.version", "product_version" ),
            ( "@hostname", "import_device_id" ),
        ),
        "filter": {
            "software.packages:*.package_type": "reg_uninstall", # nur aus registry
            },
        "converter": {},
    },
    "inv_raw_file": {
        "columns": (
            ( "@hostname", "import_device_id" ),
            ( "software.packages:*.name", "file_name" ),
            ( "software.packages:*.size", "file_size" ),
            ( "software.packages:*.path", "file_path" ),
            ( "software.packages:*.vendor", "publisher" ),
            ( "software.packages:*.summary", "product" ),
            ( "software.packages:*.version", "product_version" ),
        ),
        "filter": {
            "software.packages:*.package_type": "exe", # nur exe files
            },
        "converter": {},
    },
    "inv_raw_generic(OS)": {
        "columns": (
            ( "software.os.name", "generic_key" ),
        ),
        "filter": {},
        "converter": {},
    },
    "inv_raw_generic(Linux)": {
        "columns": (
            ( "software.packages:*.name", "name" ),
            ( "software.packages:*.version", "product_version" ),
            ( "@hostname", "import_device_id" ),
        ),
        "filter": {
            "software.packages:*.package_type": "deb", # nur exe files
            },
        "converter": {},
    },
}

import os, sys, re, time

omd_root = os.environ["OMD_ROOT"]

# both directories need to have a trailing slash "/" !
inv_dir = "%s/var/check_mk/inventory/" % omd_root
out_dir = "/var/tmp/"

if not omd_root:
    print "This script is only executable as site user"
    sys.exit(1)

def is_list(relation):
    list_start = ""
    if type(relation) == dict: # filter and converter are dicts, check them too
        relation = relation.keys()
    for field in relation:
        if not field.startswith("@"):
            if ":*" in field:
                is_list = True
                list_start = field.split(":")[0]
            else:
                is_list = False
            break
    for field in relation:
        if ( is_list != (":*" in field) or not field.startswith(list_start) ) and not field.startswith("@"):
            print "bad definition of relation, must be list or dict, not both"
            sys.exit(1)
    return list_start

def filt_it(package, relation):
    filt_start = is_list(relation["filter"])
    elements = [col[0] for col in relation["columns"]]
    list_start = is_list(elements)
    if filt_start != list_start: # do not filter if filter does not fit
        return False
    for field in relation["filter"].keys():
        if field:
            should_be = relation["filter"][field]
            field = re.sub(list_start+":\*.", "", field)
            for item in field.split("."):
                value = package[item]
                if type(value) in (str, int, float) and value == should_be:
                    return False
    return True

def convert_it(c_relation, item, field):
    for c_field in c_relation.keys():
        if c_field == field:
            item = c_relation[field](item) # apply the function defined to item
    return item

def print_line(out_rel, items):
    outtxt = "\", \"".join(map(str,items))
    out_rel.write("\"")
    out_rel.write("%s" % outtxt)
    out_rel.write("\"\n")

# special values starting with a "@"
def special_value(item, hostname):
    if item == "@hostname":
        return hostname
    elif item == "@inventory_date":
        return inventory_date[hostname]
    else:
        return ""

# extract all data
all_data = {}
inventory_date = {}
for hostname in os.listdir(inv_dir):
    fn = inv_dir + hostname
    if os.path.isfile(fn):
        a = eval(open(fn,'r').read())
	all_data[hostname] = a
	inventory_date[hostname] =  os.path.getmtime(fn)

# loop over all relations, create an output file for each relation
for ofs in relations:
    ofn = out_dir + ofs
    out_rel = open(ofn,'w')
    titles = [col[1] for col in relations[ofs]["columns"]]
    print_line(out_rel, titles)
    elements = [col[0] for col in relations[ofs]["columns"]]
    list_start = is_list(elements)
    print "starting", ofn
    if list_start == "":
        for hostname in all_data:
            items = []
            for field in elements:
                if field:
                    subtree = all_data[hostname]
                    for item in field.split("."):
                        if item.startswith("@"): # take subtree vom special_value
                            subtree = special_value(item,hostname)
                        else:
                            try:
                                subtree = subtree[item]
                            except:
                                items.append("")
                                break
                        if type(subtree) in (str, int, float):
                            converted = convert_it(relations[ofs]['converter'], subtree,field)
                            items.append(converted)
                else:
                    items.append("")
            print_line(out_rel, items)
        out_rel.close()
    else:
        for hostname in all_data:
            subtree = all_data[hostname]
            for item in list_start.split("."):
                try:
                    subtree = subtree[item]
                except:
                    print "   %s does not exist in database of host" % item
            if type(subtree) == list:
                for package in subtree:
                    if filt_it(package, relations[ofs]):
                        continue
                    items = []
                    for field in elements:
                        if field:
                            field = re.sub(list_start+":\*.", "", field)
                            for item in field.split("."):
                                if item.startswith("@"): # take subtree vom special_value
                                    value = special_value(item,hostname)
                                else:
                                    try:
                                        value = package[item]
                                    except:
                                        items.append("")
                                        break
                                if type(value) in (str, int, float):
                                    items.append(value)
                        else:
                            items.append("")
                    print_line(out_rel, items)
        out_rel.close()