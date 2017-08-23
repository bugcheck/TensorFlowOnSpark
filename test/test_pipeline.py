import numpy as np
import os
import shutil
import test
import unittest
from tensorflowonspark.pipeline import HasBatchSize, HasSteps, Namespace, TFEstimator, TFParams

def _map_fun(args, ctx):
  """Basic linear regression in a distributed TF cluster and InputMode.SPARK"""
  import tensorflow as tf
  from tensorflowonspark import TFNode
  cluster, server = TFNode.start_cluster_server(ctx)
  if ctx.job_name == "ps":
    server.join()
  elif ctx.job_name == "worker":
    with tf.device(tf.train.replica_device_setter(
      worker_device="/job:worker/task:%d" % ctx.task_index,
      cluster=cluster)):
      x = tf.placeholder(tf.float32, [None, 2], name='x')
      y_ = tf.placeholder(tf.float32, [None, 1], name='y_')
      w = tf.Variable(tf.truncated_normal([2,1]), name='w')
      y = tf.matmul(x, w, name='y')
      y2 = tf.square(y, name="y2")                      # extra/optional output for testing multiple output tensors

      cost = tf.reduce_mean(tf.square(y_ - y), name='cost')
      optimizer = tf.train.GradientDescentOptimizer(0.5).minimize(cost)

      init_op = tf.global_variables_initializer()
      saver = tf.train.Saver()

    sv = tf.train.Supervisor(is_chief=(ctx.task_index == 0),
                            init_op=init_op)
    with sv.managed_session(server.target) as sess:
      tf_feed = TFNode.DataFeed(ctx.mgr, input_mapping=args.input_mapping)
      while not sv.should_stop() and not tf_feed.should_stop():
        batch = tf_feed.next_batch(10)
        if args.input_mapping:
          if len(batch['x']) > 0:
            feed = { x: batch['x'], y_: batch['y_'] }
          opt = sess.run(optimizer, feed_dict=feed)

      if sv.is_chief:
        if args.model_dir:
          # manually save checkpoint
          ckpt_name = args.model_dir + "/model.ckpt"
          print("Saving checkpoint to: {}".format(ckpt_name))
          saver.save(sess, ckpt_name)
        elif args.export_dir:
          # export a saved_model
          signatures = {
            'test_key': {
              'inputs': { 'features': x },
              'outputs': { 'prediction': y },
              'method_name': 'test'
            }
          }
          TFNode.export_saved_model(sess, export_dir=args.export_dir, tag_set='test_tag', signatures=signatures)
        else:
          print("WARNING: model state not saved.")

    sv.stop()

class PipelineTest(test.SparkTest):
  @classmethod
  def setUpClass(cls):
    super(PipelineTest, cls).setUpClass()

    # create an artificial training dataset of two features with labels computed from known weights
    np.random.seed(1234)
    cls.features = np.random.rand(1000,2)
    cls.weights = np.array([3.14, 1.618])
    cls.labels = np.matmul(cls.features, cls.weights)
    # convert to Python types for use with Spark DataFrames
    cls.train_examples = [ (cls.features[i].tolist(), [cls.labels[i].item()]) for i in range(1000) ]
    # create a simple test dataset
    cls.test_examples = [ ([1.0, 1.0], [0.0]) ]

    # define model_dir and export_dir for tests
    cls.model_dir = os.getcwd() + os.sep + "test_model"
    cls.export_dir = os.getcwd() + os.sep + "test_export"

  @classmethod
  def tearDownClass(cls):
    super(PipelineTest, cls).tearDownClass()

  def setUp(self):
    pass

  def tearDown(self):
    # remove test artifacts
    shutil.rmtree(self.model_dir, ignore_errors=True)
    shutil.rmtree(self.export_dir, ignore_errors=True)

  def test_namespace(self):
    """Namespace class from dict"""
    d = { 'string': 'foo', 'integer': 1, 'float': 3.14, 'array': [1,2,3], 'map': {'a':1, 'b':2} }
    n = Namespace(d)
    self.assertEqual(n.string, 'foo')
    self.assertEqual(n.integer, 1)
    self.assertEqual(n.float, 3.14)
    self.assertEqual(n.array, [1,2,3])
    self.assertEqual(n.map, {'a':1, 'b':2})

  def test_TFParams(self):
    """Merging namespace args w/ ML Params"""
    class Foo(TFParams, HasBatchSize, HasSteps):
      def __init__(self, args):
        super(Foo, self).__init__()
        self.args = args

    n = Namespace({ 'a': 1, 'b': 2 })
    f = Foo(n).setBatchSize(10).setSteps(100)
    combined_args = f._merge_args_params()
    expected_args = Namespace({ 'a': 1, 'b': 2, 'batch_size': 10, 'steps': 100 })
    self.assertEqual(combined_args, expected_args)

  def test_checkpoint(self):
    """TFEstimator + TFModel using model checkpoint"""

    # create a Spark DataFrame of training examples (features, labels)
    trainDF = self.spark.createDataFrame(self.train_examples, ['col1', 'col2'])

    # train model
    args = {}
    estimator = TFEstimator(_map_fun, args) \
                  .setInputMapping( { 'col1': 'x', 'col2': 'y_' }) \
                  .setModelDir(self.model_dir) \
                  .setClusterSize(self.num_workers) \
                  .setNumPS(1) \
                  .setBatchSize(10) \
                  .setEpochs(2)
    model = estimator.fit(trainDF)
    self.assertTrue(os.path.isdir(self.model_dir))

    # create a Spark DataFrame of test examples (features, labels)
    testDF = self.spark.createDataFrame(self.test_examples, ['c1', 'c2'])

    # test model from checkpoint, referencing tensors directly
    model.setInputMapping( { 'c1': 'x' }) \
        .setOutputMapping( { 'y': 'cout' })
    preds = model.transform(testDF).head()                # take first/only result, e.g. [ Row(cout=[4.758000373840332])]
    pred = preds.cout[0]                                  # unpack scalar from tensor
    self.assertAlmostEqual(pred, np.sum(self.weights), 5)

  def test_saved_model(self):
    """TFEstimator + TFModel using saved_model export"""

    # create a Spark DataFrame of training examples (features, labels)
    trainDF = self.spark.createDataFrame(self.train_examples, ['col1', 'col2'])

    # train model
    args = {}
    estimator = TFEstimator(_map_fun, args) \
                  .setInputMapping( { 'col1': 'x', 'col2': 'y_' }) \
                  .setExportDir(self.export_dir) \
                  .setClusterSize(self.num_workers) \
                  .setNumPS(1) \
                  .setBatchSize(10) \
                  .setEpochs(2)
    model = estimator.fit(trainDF)
    self.assertTrue(os.path.isdir(self.export_dir))

    # create a Spark DataFrame of test examples (features, labels)
    testDF = self.spark.createDataFrame(self.test_examples, ['c1', 'c2'])

    # test saved_model using exported signature
    model.setTagSet('test_tag') \
          .setSignatureDefKey('test_key') \
          .setInputMapping({ 'c1': 'features' }) \
          .setOutputMapping({ 'prediction': 'cout' })
    preds = model.transform(testDF).head()                  # take first/only result
    pred = preds.cout[0]                                    # unpack scalar from tensor
    expected = np.sum(self.weights)
    self.assertAlmostEqual(pred, expected, 5)

    # test saved_model using custom/direct mapping
    model.setTagSet('test_tag') \
          .setSignatureDefKey(None) \
          .setInputMapping({ 'c1': 'x'}) \
          .setOutputMapping({ 'y': 'cout1', 'y2': 'cout2' })
    preds = model.transform(testDF).head()                  # take first/only result
    pred = preds.cout1[0]                                   # unpack pred scalar from tensor
    squared_pred = preds.cout2[0]                           # unpack squared pred from tensor

    self.assertAlmostEqual(pred, expected, 5)
    self.assertAlmostEqual(squared_pred, expected * expected, 5)


if __name__ == '__main__':
  unittest.main()