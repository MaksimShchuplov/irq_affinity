#!/usr/bin/python
# -*- coding: UTF-8 -*-

import os

__author__ = "Maksim Shchuplov shchuplov@gmail.com"


if __name__ == "__main__":
    file = open('/proc/interrupts', "r")
    cpukol = len(file.readline().split())
    print "cpuused : " + str(cpukol)
    file.close()

class bcolors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'

    def disable(self):
        self.HEADER = ''
        self.OKBLUE = ''
        self.OKGREEN = ''
        self.WARNING = ''
        self.FAIL = ''
        self.ENDC = ''

cpuirqmass = ["1", "2", "4", "8", "10", "20", "40", "80", "100", "200", "400", "800", "1000", "2000", "4000", "8000",
              "10000", "20000", "40000", "80000", "100000", "200000", "4000000", "800000"]



file = open('/proc/interrupts', "r")
interruptsmass = []

for i in file:
    interruptsmass.append(str(i))
file.close()
devhash = {}
irqbalanc_enabled = 1
chkconfig_irq_disable = 1
for string in interruptsmass:
    #eth
    if "eth" in string:
        if irqbalanc_enabled:
            os.system("killall irqbalance")
            irqbalanc_enabled = 0
        if chkconfig_irq_disable:
        #           os.system("rm /etc/cron.hourly/irq.py")
        #           os.system("chkconfig --del irq.py")
        #           os.system("rm /etc/init.d/irq.py")
            os.system("chkconfig irq on")
            chkconfig_irq_disable = 0

        devname = string.split()[-1].split("-")[0]
        if devname + "-" in string:
            if devname not in devhash:
                irqmass = []
                irqmass.append(int(string.split(":")[0]))

                devhash[devname] = irqmass
            else:
                irqmass = devhash[devname]
                irqmass.append(int(string.split(":")[0]))
                devhash[devname] = irqmass

    #megasas
    if "megasas" in string:
        devname = string.split()[-1].split("-")[0]
        if devname not in devhash:
            irqmass = []
            irqmass.append(int(string.split(":")[0]))

            devhash[devname] = irqmass
        else:
            irqmass = devhash[devname]
            irqmass.append(int(string.split(":")[0]))
            devhash[devname] = irqmass

for i in devhash:
# irq-cpu
    print i, str(devhash[i])
    mass = devhash[i]
    kol = int(0)
    for irq in mass:
        intfile = open("/proc/irq/" + str(irq) + "/smp_affinity", "r")

        # print "should be : echo " + str(cpuirqmass[kol]) + " > /proc/irq/" + str(irq) + "/smp_affinity"
        if str(cpuirqmass[kol]) not in intfile.readline():
            print "setting up irq " + str(irq) + " to CPU core " + str(
                kol) + "................" + bcolors.OKGREEN + "[OK!]" + bcolors.ENDC
            os.system("echo " + str(cpuirqmass[kol]) + " > /proc/irq/" + str(irq) + "/smp_affinity")
        else:
            print "setting up irq " + str(irq) + " to CPU core " + str(
                kol) + "................" + bcolors.OKGREEN + "[already set]" + bcolors.ENDC
        intfile.close()

        if kol < len(cpuirqmass) - 1:
            kol = kol + 1
        else:
            kol = 0
