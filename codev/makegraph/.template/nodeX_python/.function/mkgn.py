import os
import yaml
INPUT = yaml.safe_load(os.environ['MKGN_INPUT'])
OUTPUT = open(os.environ['MKGN_OUTPUT'],'w')
NAME = os.environ['MKGN_NAME']


