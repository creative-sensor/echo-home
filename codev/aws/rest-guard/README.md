This page present how to install rest-guard utility

Rest-guard periodically check current active usage and decide to put OS in rest state to save EC2 cost.
Currently rest condition is determined by active network connection (tcp/443)

### 0 INPUT
Edit file ```INPUT-SET.remote```:
* ORG_NAME: name of base dir to put script in
* FIRST_REST: The minute at which rest is triggered the first time since bootup
* PERIODIC_REST: The minute interval between 2 consecutive rest checks

### 1 Install
```
bash> make install
```


### 2 Remove
```
bash> make clean
```
