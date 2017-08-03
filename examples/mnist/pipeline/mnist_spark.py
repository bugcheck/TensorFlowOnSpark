# Copyright 2017 Yahoo Inc.
# Licensed under the terms of the Apache 2.0 license.
# Please see LICENSE file in the project root for terms.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from pyspark.conf import SparkConf
from pyspark.context import SparkContext
from pyspark.sql import SparkSession

import argparse
import os
import numpy
import sys
import tensorflow as tf
import threading
import time
from datetime import datetime

from tensorflowonspark import TFCluster
from tensorflowonspark.pipeline import TFEstimator, TFModel
import mnist_dist

sc = SparkContext(conf=SparkConf().setAppName("mnist_spark"))
spark = SparkSession(sc)

executors = sc._conf.get("spark.executor.instances")
num_executors = int(executors) if executors is not None else 1
num_ps = 1

parser = argparse.ArgumentParser()

######## PARAMS ########

## TFoS/cluster
parser.add_argument("--batch_size", help="number of records per batch", type=int, default=100)
parser.add_argument("--epochs", help="number of epochs", type=int, default=1)
parser.add_argument("--model_dir", help="HDFS path to save/load model during train/inference", default="mnist_model")
parser.add_argument("--export_dir", help="HDFS path to export model", default="mnist_export")
parser.add_argument("--cluster_size", help="number of nodes in the cluster", type=int, default=num_executors)
parser.add_argument("--num_ps", help="number of PS nodes in cluster", type=int, default=1)
parser.add_argument("--rdma", help="use rdma connection", action="store_true")
parser.add_argument("--steps", help="maximum number of steps", type=int, default=1000)
parser.add_argument("--tensorboard", help="launch tensorboard process", action="store_true")

######## ARGS ########

# Spark input/output
parser.add_argument("--format", help="example format: (csv|pickle|tfr)", choices=["csv","pickle","tfr"], default="csv")
parser.add_argument("--images", help="HDFS path to MNIST images in parallelized format")
parser.add_argument("--labels", help="HDFS path to MNIST labels in parallelized format")
parser.add_argument("--output", help="HDFS path to save test/inference output", default="predictions")

args = parser.parse_args()
print("args:",args)

print("{0} ===== Start".format(datetime.now().isoformat()))

if args.format == "tfr":
  images = sc.newAPIHadoopFile(args.images, "org.tensorflow.hadoop.io.TFRecordFileInputFormat",
                              keyClass="org.apache.hadoop.io.BytesWritable",
                              valueClass="org.apache.hadoop.io.NullWritable")
  def toNumpy(bytestr):
    example = tf.train.Example()
    example.ParseFromString(bytestr)
    features = example.features.feature
    image = numpy.array(features['image'].int64_list.value)
    label = numpy.array(features['label'].int64_list.value)
    return (image, label)
  dataRDD = images.map(lambda x: toNumpy(str(x[0])))
else:
  if args.format == "csv":
    images = sc.textFile(args.images).map(lambda ln: [int(x) for x in ln.split(',')])
    labels = sc.textFile(args.labels).map(lambda ln: [float(x) for x in ln.split(',')])
  else: # args.format == "pickle":
    images = sc.pickleFile(args.images)
    labels = sc.pickleFile(args.labels)
  print("zipping images and labels")
  dataRDD = images.zip(labels)

# Pipeline API
df = spark.createDataFrame(dataRDD)

print("{0} ===== Estimator.fit()".format(datetime.now().isoformat()))
# dummy tf args (from imagenet/inception example)
tf_args = { 'initial_learning_rate': 0.045, 'num_epochs_per_decay': 2.0, 'learning_rate_decay_factor': 0.94 }
estimator = TFEstimator(mnist_dist.map_fun, tf_args) \
        .setModelDir(args.model_dir) \
        .setExportDir(args.export_dir) \
        .setClusterSize(args.cluster_size) \
        .setNumPS(args.num_ps) \
        .setRDMA(args.rdma) \
        .setTensorboard(args.tensorboard) \
        .setEpochs(args.epochs) \
        .setBatchSize(args.batch_size) \
        .setSteps(args.steps)

model = estimator.fit(df)

# prediction
model.setTagSet(tf.saved_model.tag_constants.SERVING) \
      .setSignatureDefKey(tf.saved_model.signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY) \
      .setInputTensor('input') \
      .setOutputTensor('output')

# featurize
#model.setTagSet(tf.saved_model.tag_constants.SERVING) \
#      .setSignatureDefKey('featurize') \
#      .setInputTensor('images') \
#      .setOutputTensor('features')

print("{0} ===== Model.transform()".format(datetime.now().isoformat()))
test_data = spark.createDataFrame(images)
preds = model.transform(test_data)
preds.write.text(args.output)

print("{0} ===== Stop".format(datetime.now().isoformat()))
