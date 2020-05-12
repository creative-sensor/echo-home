#!/usr/bin/env python
import os , sys , subprocess , time

cmd = []
my_pid = os.getpid()

if len(sys.argv) == 1:
    cmd = ["/usr/bin/pstree" , "-s" ,  "-p" , str(my_pid)]
else:
    cmd = sys.argv[1:]


### CREATE FIRST CHILD PROCESS WITH PIPE AS DEFAULT
output = subprocess.check_output(cmd, shell=False)
output = output.decode("utf-8")
print(output)
#pstree -s -p 3563:
    #systemd(1)---systemd(1605)---gnome-terminal-(2449)---bash(2683)---python(3563)---pstree(3564)



### CREATE MY SLEEPING CHILD PROCESS USING POPEN INTERFACE (PIPE IS OPTIONAL)
child = subprocess.Popen(["sleep", "60" ],  shell=False, stdout=None)
#pstree -s -p 3465:
    #systemd(1)───systemd(1605)───gnome-terminal-(2449)───bash(2683)───python(3463)───sleep(3465)



### CHILD WILL BE ADOPTED WHEN ITS PARENT IS DEAD
print("I[" + str(my_pid) + "] am dying soon and my sleeping child[" + str(child.pid) + "] will be adopted by systemd")
#pstree -s -p 3465:
    #systemd(1)───systemd(1605)───sleep(3465)
time.sleep(30)





