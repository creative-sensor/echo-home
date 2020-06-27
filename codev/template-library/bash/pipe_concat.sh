#!/bin/bash -x

stage_0="ls -la /"
stage_1="cat"
stage_2="grep home"
pipeline="$stage_0 | $stage_1 | $stage_2"

bash -c "$pipeline"
