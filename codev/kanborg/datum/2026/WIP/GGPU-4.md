# gpustack : distributed inference
--------------------------------
### 0 DESCRIPTION

https://github.com/gpustack/gpustack?tab=readme-ov-file


### 1 SOLUTION


### 2 NOTES


### 3 TEST/VERIFICATION


### 4 JOURNAL

```
mlx.launch --backend mpi --env MLX_METAL_FAST_SYNCH=1  --hostfile host.json sharded_generate.py   --prompt 'Hello world' --model "$HOME/.lmstudio/models/mlx-community/gemma-4-26b-a4b-mxfp4/"
Traceback (most recent call last):
  File "$HOME/echo-home/workstation/macos/mlx-lm/mlx_lm/examples/sharded_generate.py", line 63, in <module>
    model, tokenizer = sharded_load(args.model, pipeline_group, tensor_group)
                       ~~~~~~~~~~~~^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/Library/Frameworks/Python.framework/Versions/3.13/lib/python3.13/site-packages/mlx_lm/utils.py", line 540, in sharded_load
    raise ValueError(
        "The model does not support tensor parallelism but a tensor_group was provided"
    )
ValueError: The model does not support tensor parallelism but a tensor_group was provided
--------------------------------------------------------------------------
prterun detected that one or more processes exited with non-zero status,
thus causing the job to be terminated. The first process to do so was:

   Process name: [prterun-$HOSTNAME-71842@1,0]
   Exit code:    1
```


--------------------------------
```json
{ "project_code": "GGPU" , "links": "" , "location": "" , "fpoint": "" }
```
