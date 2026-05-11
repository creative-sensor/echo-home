# gitlab : mono service
--------------------------------
### 0 DESCRIPTION
- run in docker container
- start in detached mode
- can invoke multiple instance from the same objectory (optional)

### 1 SOLUTION

- https://github.com/creative-sensor/echo-home/commit/4adfe31a5feb24ad2f46b2d1aae149de2b112a2d

### 2 NOTES

- TODO: can invoke multiple instance from the same objectory

### 3 TEST/VERIFICATION


### 4 JOURNAL

###### 4.2
- Need to add DNS config to VARSET and use only when consul dns is available

- Config gitlab.rb to be edited:
```ruby
external_url 'http://gitlab.service.vector.consul'
gitlab_rails['monitoring_whitelist'] = ['127.0.0.0/8', '::1/128', '172.0.0.0/8']
```
- Log of pipeline test run
```bash
[0KRunning with gitlab-runner 15.9.0 (c20f0bec)[0;m
[0K  on runner-docker mgz7V9NZ, system ID: r_N4E6Vxd4NGE4[0;m
section_start:1677033331:prepare_executor
[0K[0K[36;1mPreparing the "docker" executor[0;m[0;m
[0KUsing Docker executor with image busybox:latest ...[0;m
[0KPulling docker image busybox:latest ...[0;m
[0KUsing docker image sha256:66ba00ad3de8677a3fa4bc4ea0fc46ebca0f14db46ca365e7f60833068dd0148 for busybox:latest with digest busybox@sha256:7b3ccabffc97de872a30dfd234fd972a66d247c8cfc69b0550f276481852627c ...[0;m
section_end:1677033334:prepare_executor
[0Ksection_start:1677033334:prepare_script
[0K[0K[36;1mPreparing environment[0;m[0;m
Running on runner-mgz7v9nz-project-3-concurrent-0 via gitlab-runner.docker...
section_end:1677033335:prepare_script
[0Ksection_start:1677033335:get_sources
[0K[0K[36;1mGetting source from Git repository[0;m[0;m
[32;1mFetching changes with git depth set to 20...[0;m
Initialized empty Git repository in /builds/gitlab-instance-5fd3f44b/sgp/.git/
[32;1mCreated fresh repository.[0;m
[32;1mChecking out b7e9136e as detached HEAD (ref is master)...[0;m

[32;1mSkipping Git submodules setup[0;m
section_end:1677033336:get_sources
[0Ksection_start:1677033336:step_script
[0K[0K[36;1mExecuting "step_script" stage of the job script[0;m[0;m
[0KUsing docker image sha256:66ba00ad3de8677a3fa4bc4ea0fc46ebca0f14db46ca365e7f60833068dd0148 for busybox:latest with digest busybox@sha256:7b3ccabffc97de872a30dfd234fd972a66d247c8cfc69b0550f276481852627c ...[0;m
[32;1m$ echo "Before script section"[0;m
Before script section
[32;1m$ echo "For example you might run an update here or install a build dependency"[0;m
For example you might run an update here or install a build dependency
[32;1m$ echo "Or perhaps you might print out some debugging details"[0;m
Or perhaps you might print out some debugging details
[32;1m$ echo "Do your build here"[0;m
Do your build here
section_end:1677033336:step_script
[0Ksection_start:1677033336:after_script
[0K[0K[36;1mRunning after_script[0;m[0;m
[32;1mRunning after script...[0;m
[32;1m$ echo "After script section"[0;m
After script section
[32;1m$ echo "For example you might do some cleanup here"[0;m
For example you might do some cleanup here
section_end:1677033337:after_script
[0K[32;1mJob succeeded[0;m
```
###### 4.1
- to configure every child gitlab runner, pod, container with  consul dns resolver


###### 4.0
- gitlab
- gitlab-runner.register()
- Need dns service to avoid usage of etc_host

--------------------------------
```json
{ "project_code": "LEAF" , "links": "" , "location": "" , "fpoint": "" }
```
