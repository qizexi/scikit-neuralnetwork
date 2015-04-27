from __future__ import (absolute_import, unicode_literals, print_function)

__all__ = ['MultiLayerPerceptronRegressor', 'MultiLayerPerceptronClassifier']

import os
import time
import logging
import itertools

log = logging.getLogger('sknn')


# By default, we force Theano to use a GPU and fallback to CPU, using 32-bits.
# This must be done in the code before Theano is imported for the first time.
os.environ['THEANO_FLAGS'] = "device=gpu,floatX=float32"

cuda = logging.getLogger('theano.sandbox.cuda')
cuda.setLevel(logging.CRITICAL)
import theano
cuda.setLevel(logging.WARNING)


import numpy
import sklearn.base
import sklearn.pipeline
import sklearn.preprocessing
import sklearn.cross_validation

from pylearn2.datasets import DenseDesignMatrix
from pylearn2.training_algorithms import sgd
from pylearn2.models import mlp, maxout
from pylearn2.costs.mlp.dropout import Dropout
from pylearn2.training_algorithms.learning_rule import RMSProp, Momentum, AdaGrad, AdaDelta
from pylearn2.space import Conv2DSpace
from pylearn2.termination_criteria import MonitorBased


class ansi:
    BOLD = '\033[1;97m'
    WHITE = '\033[0;97m'
    BLUE = '\033[0;94m'
    GREEN = '\033[0;32m'
    ENDC = '\033[0m'


class Layer(object):

    def __init__(
            self,
            type,
            nop=None,
            name=None,
            units=None,
            pieces=None,
            channels=None,
            kernel_shape=None,
            pool_shape=None,
            pool_type=None,
            dropout=None):
        """
        Parameters
        ----------

        type: str
            Select which activation function this layer should use, as a string.
                * For hidden layers, you can use the following layer types:
                ``Rectifier``, ``Sigmoid``, ``Tanh``, ``Maxout`` or ``Convolution``.
                * For output layers, you can use the following layer types:
                ``Linear``, ``Softmax`` or ``Gaussian``.

        name: str, optional
            You optionally can specify a name for this layer, and its parameters
            will then be accessible to `scikit-learn` via a nested sub-object.  For example,
            if name is set to `hidden1`, then the parameter `hidden1__units` from the network
            is bound to this layer's `units` variable.

        units: int, optional
            The number of units (also known as neurons) in this layer.  This applies to all
            layer types except for convolution.

        pieces: int, optional
            The number of piecewise linear segments in the Maxout activation.  This is
            optional and only applies when `Maxout` is selected as the layer type.

        channels: int, optional
            Number of output channels for the convolution layers.  Each channel has its own
            set of shared weights which are trained by applying the kernel over the image.

        kernel_shape: tuple of ints, optional
            A two-dimensional tuple of integers corresponding to the shape of the kernel when
            convolution is used.  For example, this could be a square kernel `(3,3)` or a full
            horizontal or vertical kernel on the input matrix, e.g. `(N,1)` or `(1,N)`.

        pool_shape: tuple of ints, optional
            A two-dimensional tuple of integers corresponding to the pool size.  This should be
            square, for example `(2,2)` to reduce the size by half, or `(4,4)` to make the output
            a quarter of the original.

        pool_type: str, optional
            Type of the pooling to be used; can be either `max` or `mean`.  The default is 
            to take the maximum value of all inputs that fall into this pool.

        dropout: float, optional
            The ratio of inputs to drop out for this layer during training.  For example, 0.25
            means that 25% of the inputs will be excluded for each training sample, with the
            remaining inputs being renormalized accordingly.
        """
        assert nop is None,\
            "Specify layer parameters as keyword arguments, not positional arguments."

        if type not in ['Rectifier', 'Sigmoid', 'Tanh', 'Maxout', 'Convolution',
                        'Linear', 'Softmax', 'Gaussian']:
            raise NotImplementedError("Layer type `%s` is not implemented." % type)

        self.name = name
        self.type = type
        self.units = units
        self.pieces = pieces
        self.channels = channels
        self.kernel_shape = kernel_shape
        self.pool_shape = pool_shape
        self.pool_type = pool_type
        self.dropout = dropout

    def __eq__(self, other):
        return self.__dict__ == other.__dict__

    def __repr__(self):
        params = ", ".join(["%s=%r" % (k, v) for k, v in self.__dict__.items() if v is not None])
        return "<sknn.mlp.Layer %s: %s>" % (self.type, params)


class BaseMLP(sklearn.base.BaseEstimator):
    """
    Abstract base class for wrapping the multi-layer perceptron functionality
    from PyLearn2.

    Parameters
    ----------
    layers : list[Layer]
        An iterable sequence of each layer each as a Layer instance that contains
        its type, optional name, and any paramaters required.

            * For hidden layers, you can use the following layer types:
              ``Rectifier``, ``Sigmoid``, ``Tanh``, ``Maxout`` or ``Convolution``.
            * For output layers, you can use the following layer types:
              ``Linear``, ``Softmax`` or ``Gaussian``.

        You must specify exactly one output layer type, so the last entry in your
        ``layers`` list should contain ``Linear`` for regression, or ``Softmax`` for
        classification (recommended).

    random_state : int
        Seed for the initialization of the neural network parameters (e.g.
        weights and biases).  This is fully deterministic.

    learning_rule : str
        Name of the learning rule used during stochastic gradient descent,
        one of ``sgd``, ``momentum``, ``nesterov``, ``adadelta`` or ``rmsprop``
        at the moment.

    learning_rate : float
        Real number indicating the default/starting rate of adjustment for
        the weights during gradient descent.  Different learning rules may
        take this into account differently.

    learning_momentum : float
        Real number indicating the momentum factor to be used for the
        learning rule 'momentum'.

    batch_size : int
        Number of training samples to group together when performing stochastic
        gradient descent.  By default each sample is treated on its own.

    n_iter : int
        The number of iterations of gradient descent to perform on the
        neural network's weights when training with ``fit()``.

    valid_set : tuple of array-like
        Validation set (X_v, y_v) to be used explicitly while training.  Both
        arrays should have the same size for the first dimention, and the second
        dimention should match with the training data specified in ``fit()``.

    valid_size : float
        Ratio of the training data to be used for validation.  0.0 means no
        validation, and 1.0 would mean there's no training data!  Common values are
        0.1 or 0.25.

    n_stable : int
        Number of interations after which training should return when the validation
        error remains constant.  This is a sign that the data has been fitted.

    f_stable : float
        Threshold under which the validation error change is assumed to be stable, to
        be used in combination with `n_stable`.

    dropout : bool or float
        Whether to use drop-out training for the inputs (jittering) and the
        hidden layers, for each training example. If a float is specified, that
        ratio of inputs will be randomly excluded during training (e.g. 0.5).

    verbose : bool
        If True, print the score at each epoch via the logger called 'sknn'.  You can
        control the detail of the output by customising the logger level and formatter.
    """

    def __init__(
            self,
            layers,
            random_state=None,
            learning_rule='sgd',
            learning_rate=0.01,
            learning_momentum=0.9,
            dropout=False,
            batch_size=1,
            n_iter=None,
            n_stable=50,
            f_stable=0.001,
            valid_set=None,
            valid_size=0.0,            
            verbose=False):

        self.layers = []
        for i, layer in enumerate(layers):
            assert isinstance(layer, Layer),\
                "Specify each layer as an instance of a `sknn.mlp.Layer` object."

            if layer.name is None:
                label = "Hidden" if i < len(layers)-1 else "Output"
                layer.name = "%s_%i_%s" % (label, i, layer.type)

            self.layers.append(layer)

        self.random_state = random_state
        self.learning_rule = learning_rule
        self.learning_rate = learning_rate
        self.learning_momentum = learning_momentum
        self.dropout = dropout if type(dropout) is float else (0.5 if dropout else 0.0)
        self.batch_size = batch_size
        self.n_iter = n_iter
        self.n_stable = n_stable
        self.f_stable = f_stable
        self.valid_set = valid_set
        self.valid_size = valid_size
        self.verbose = verbose

        self.unit_counts = None
        self.input_space = None
        self.mlp = None
        self.weights = None
        self.vs = None
        self.ds = None
        self.trainer = None
        self.f = None
        self.train_set = None
        self.best_valid_error = float("inf")

        self.cost = "Dropout" if dropout else None
        if learning_rule == 'sgd':
            self._learning_rule = None
        # elif learning_rule == 'adagrad':
        #     self._learning_rule = AdaGrad()
        elif learning_rule == 'adadelta':
            self._learning_rule = AdaDelta()
        elif learning_rule == 'momentum':
            self._learning_rule = Momentum(learning_momentum)
        elif learning_rule == 'nesterov':
            self._learning_rule = Momentum(learning_momentum, nesterov_momentum=True)
        elif learning_rule == 'rmsprop':
            self._learning_rule = RMSProp()
        else:
            raise NotImplementedError(
                "Learning rule type `%s` is not supported." % learning_rule)

        self._setup()

    def _setup(self):
        # raise NotImplementedError("BaseMLP is an abstract class; "
        #                           "use the Classifier or Regressor instead.")
        pass

    def _create_trainer(self, dataset):
        sgd.log.setLevel(logging.WARNING)

        # Aggregate all the dropout parameters into shared dictionaries.
        probs, scales = {}, {}
        for l in [l for l in self.layers if l.dropout is not None]:
            incl = 1.0 - l.dropout
            probs[l.name] = incl
            scales[l.name] = 1.0 / incl

        if self.cost == "Dropout" or len(probs) > 0:
            # Use the globally specified dropout rate when there are no layer-specific ones.
            incl = 1.0 - self.dropout
            default_prob, default_scale = incl, 1.0 / incl

            # Pass all the parameters to pylearn2 as a custom cost function.
            self.cost = Dropout(
                default_input_include_prob=default_prob,
                default_input_scale=default_scale,
                input_include_probs=probs, input_scales=scales)

        logging.getLogger('pylearn2.monitor').setLevel(logging.WARNING)
        if dataset is not None:
            termination_criterion = MonitorBased(
                channel_name='objective',
                N=self.n_stable,
                prop_decrease=self.f_stable)
        else:
            termination_criterion = None

        return sgd.SGD(
            cost=self.cost,
            batch_size=self.batch_size,
            learning_rule=self._learning_rule,
            learning_rate=self.learning_rate,
            termination_criterion=termination_criterion,
            monitoring_dataset=dataset)

    def _check_layer(self, layer, required, optional=[]):
        required.extend(['name', 'type'])
        for r in required:
            if getattr(layer, r) is None:
                raise ValueError("Layer type `%s` requires parameter `%s`."\
                                 % (layer.type, r))

        optional.extend(['dropout'])
        for a in layer.__dict__:
            if a in required+optional:
                continue
            if getattr(layer, a) is not None:
                log.warning("Parameter `%s` is unused for layer type `%s`."\
                            % (a, layer.type))

    def _create_hidden_layer(self, name, layer, irange=0.1):
        if layer.type == "Rectifier":
            self._check_layer(layer, ['units'])
            return mlp.RectifiedLinear(
                layer_name=name,
                dim=layer.units,
                irange=irange)

        if layer.type == "Sigmoid":
            self._check_layer(layer, ['units'])
            return mlp.Sigmoid(
                layer_name=name,
                dim=layer.units,
                irange=irange)

        if layer.type == "Tanh":
            self._check_layer(layer, ['units'])
            return mlp.Tanh(
                layer_name=name,
                dim=layer.units,
                irange=irange)

        if layer.type == "Maxout":
            self._check_layer(layer, ['units', 'pieces'])
            return maxout.Maxout(
                layer_name=name,
                num_units=layer.units,
                num_pieces=layer.pieces,
                irange=irange)

        if layer.type == "Convolution":
            self._check_layer(layer, ['channels', 'kernel_shape'],
                                     ['pool_shape', 'pool_type'])
            return mlp.ConvRectifiedLinear(
                layer_name=name,
                output_channels=layer.channels,
                kernel_shape=layer.kernel_shape,
                pool_shape=layer.pool_shape or (1,1),
                pool_type=layer.pool_type or 'max',
                pool_stride=(1,1),
                irange=irange)

        raise NotImplementedError(
            "Hidden layer type `%s` is not supported." % layer.type)

    def _create_output_layer(self, layer):
        fan_in = self.unit_counts[-2]
        fan_out = self.unit_counts[-1]
        lim = numpy.sqrt(6) / (numpy.sqrt(fan_in + fan_out))

        if layer.type == "Linear":
            self._check_layer(layer, ['units'])
            return mlp.Linear(
                layer_name=layer.name,
                dim=layer.units,
                irange=lim)

        if layer.type == "Gaussian":
            self._check_layer(layer, ['units'])
            return mlp.LinearGaussian(
                layer_name=layer.name,
                init_beta=0.1,
                min_beta=0.001,
                max_beta=1000,
                beta_lr_scale=None,
                dim=layer.units,
                irange=lim)

        if layer.type == "Softmax":
            self._check_layer(layer, ['units'])
            return mlp.Softmax(
                layer_name=layer.name,
                n_classes=layer.units,
                irange=lim)

        raise NotImplementedError(
            "Output layer type `%s` is not supported." % layer.type)

    def _create_mlp(self):
        # Create the layers one by one, connecting to previous.
        mlp_layers = []
        for i, layer in enumerate(self.layers[:-1]):
            fan_in = self.unit_counts[i]
            fan_out = self.unit_counts[i + 1]

            lim = numpy.sqrt(6) / numpy.sqrt(fan_in + fan_out)
            if layer.type == "Tanh":
                lim *= 1.1 * lim
            elif layer.type in ("Rectifier", "Maxout", "Convolution"):
                # He, Rang, Zhen and Sun, converted to uniform.
                lim *= numpy.sqrt(2)
            elif layer.type == "Sigmoid":
                lim *= 4

            hidden_layer = self._create_hidden_layer(layer.name, layer, irange=lim)
            mlp_layers.append(hidden_layer)

        # Deal with output layer as a special case.
        output_layer = self._create_output_layer(self.layers[-1])
        mlp_layers.append(output_layer)

        self.mlp = mlp.MLP(
            mlp_layers,
            nvis=None if self.is_convolution else self.unit_counts[0],
            seed=self.random_state,
            input_space=self.input_space)

        if self.weights is not None:
            self._array_to_mlp(self.weights, self.mlp)
            self.weights = None

        inputs = self.mlp.get_input_space().make_theano_batch()
        self.f = theano.function([inputs], self.mlp.fprop(inputs))

    def _create_matrix_input(self, X, y):
        if self.is_convolution:
            # Using `b01c` arrangement of data, see this for details:
            #   http://benanne.github.io/2014/04/03/faster-convolutions-in-theano.html
            # input: (batch size, channels, rows, columns)
            # filters: (number of filters, channels, rows, columns)
            input_space = Conv2DSpace(shape=X.shape[1:3], num_channels=X.shape[-1])
            view = input_space.get_origin_batch(X.shape[0])
            return DenseDesignMatrix(topo_view=view, y=y), input_space
        else:
            return DenseDesignMatrix(X=X, y=y), None

    def _initialize(self, X, y):
        assert not self.is_initialized,\
            "This neural network has already been initialized."

        log.info(
            "Initializing neural network with %i layers, %i inputs and %i outputs.",
            len(self.layers), X.shape[1], y.shape[1])

        # Calculate and store all layer sizes.
        if self.layers[-1].units is None:
            self.layers[-1].units = y.shape[1]
        else:
            assert self.layers[-1].units == y.shape[1],\
                "Mismatch between dataset size and units in output layer."

        self.unit_counts = [X.shape[1]]
        for layer in self.layers:
            if layer.units is not None:
                self.unit_counts.append(layer.units)
            else:
                # TODO: Compute correct number of outputs for convolution.
                self.unit_counts.append(layer.channels)

            log.debug("  - Type: {}{: <10}{}  Units: {}{: <4}{}".format(
                ansi.BOLD, layer.type, ansi.ENDC, ansi.BOLD, layer.units or "N/A", ansi.ENDC))
        log.debug("")

        if self.valid_size > 0.0:
            assert self.valid_set is None, "Can't specify valid_size and valid_set together."
            X, X_v, y, y_v = sklearn.cross_validation.train_test_split(
                                X, y,
                                test_size=self.valid_size,
                                random_state=self.random_state)
            self.valid_set = X_v, y_v
        self.train_set = X, y

        # Convolution networks need a custom input space.
        self.ds, self.input_space = self._create_matrix_input(X, y)
        if self.valid_set:
            X_v, y_v = self.valid_set
            self.vs, _ = self._create_matrix_input(X_v, y_v)
        else:
            self.vs = None

        self._create_mlp()

        self.trainer = self._create_trainer(self.vs)
        self.trainer.setup(self.mlp, self.ds)
        

    @property
    def is_initialized(self):
        """Check if the neural network was setup already.
        """
        return not (self.mlp is None or self.f is None)

    @property
    def is_convolution(self):
        """Check whether this neural network includes convolution layers.
        """
        return "Conv" in self.layers[0].type

    def __getstate__(self):
        assert self.mlp is not None,\
            "The neural network has not been initialized."

        d = self.__dict__.copy()
        d['weights'] = self._mlp_to_array()

        for k in ['ds', 'vs', 'f', 'trainer', 'mlp']:
            if k in d:
                del d[k]
        return d

    def _mlp_to_array(self):
        return [(l.get_weights(), l.get_biases()) for l in self.mlp.layers]

    def __setstate__(self, d):
        self.__dict__.update(d)
        for k in ['ds', 'vs', 'f', 'trainer', 'mlp']:
            setattr(self, k, None)
        self._create_mlp()

    def _array_to_mlp(self, array, nn):
        for layer, (weights, biases) in zip(nn.layers, array):
            assert layer.get_weights().shape == weights.shape
            layer.set_weights(weights)

            assert layer.get_biases().shape == biases.shape
            layer.set_biases(biases)

    def _fit(self, X, y, test=None):
        assert X.shape[0] == y.shape[0],\
            "Expecting same number of input and output samples."
        num_samples, data_size = X.shape[0], X.size+y.size

        if y.ndim == 1:
            y = y.reshape((y.shape[0], 1))
        if not isinstance(X, numpy.ndarray):
            X = X.toarray()
        if not isinstance(y, numpy.ndarray):
            y = y.toarray()

        if not self.is_initialized:            
            self._initialize(X, y)
            X, y = self.train_set
        else:
            self.train_set = X, y

        if self.is_convolution:
            X = self.ds.view_converter.topo_view_to_design_mat(X)
        self.ds.X, self.ds.y = X, y

        # Bug in PyLearn2 that has some unicode channels, can't sort.
        self.mlp.monitor.channels = {str(k): v for k, v in self.mlp.monitor.channels.items()}

        log.info("Training on dataset of {:,} samples with {:,} total size.".format(num_samples, data_size))
        if self.valid_set:
            X_v, _ = self.valid_set
            log.debug("  - Train: {: <9,}  Valid: {: <4,}".format(X.shape[0], X_v.shape[0]))
        if self.n_iter:
            log.debug("  - Terminating loop after {} total iterations.".format(self.n_iter))
        if self.n_stable:
            log.debug("  - Early termination after {} stable iterations.".format(self.n_stable))

        log.debug("""
Epoch    Validation Error    Time
---------------------------------""")

        for i in itertools.count(0):
            start = time.time()
            self.trainer.train(dataset=self.ds)

            self.mlp.monitor.report_epoch()
            self.mlp.monitor()

            if not self.trainer.continue_learning(self.mlp):
                log.debug("")
                log.info("Early termination condition fired at %i iterations.", i)
                break
            if self.n_iter is not None and i >= self.n_iter:
                log.debug("")
                log.info("Terminating after specified %i total iterations.", i)
                break

            if self.verbose:
                objective = self.mlp.monitor.channels.get('objective', None)
                if objective:
                    avg_valid_error = objective.val_shared.get_value()
                    self.best_valid_error = min(self.best_valid_error, avg_valid_error)
                else:
                    avg_valid_error = None

                best_valid = bool(self.best_valid_error == avg_valid_error)
                log.debug("{:>5}      {}{}{}        {:>3.1f}s".format(
                          i,
                          ansi.GREEN if best_valid else "",
                          "{:>10.6f}".format(float(avg_valid_error)) if avg_valid_error else "     N/A  ",
                          ansi.ENDC if best_valid else "",
                          time.time() - start
                          ))

        return self

    def _predict(self, X):
        if not self.is_initialized:
            raise ValueError("The neural network has not been trained.")

        if X.dtype != numpy.float32:
            X = X.astype(numpy.float32)
        if not isinstance(X, numpy.ndarray):
            X = X.toarray()

        return self.f(X)



class MultiLayerPerceptronRegressor(BaseMLP, sklearn.base.RegressorMixin):
    """Regressor compatible with sklearn that wraps PyLearn2.
    """

    def fit(self, X, y):
        """Fit the neural network to the given data.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_inputs)
            Training vectors as real numbers, where n_samples is the number of
            samples and n_inputs is the number of input features.

        y : array-like, shape (n_samples, n_outputs)
            Target values as real numbers, either as regression targets or
            label probabilities for classification.

        Returns
        -------
        self : object
            Returns this instance.
        """
        return super(MultiLayerPerceptronRegressor, self)._fit(X, y)

    def predict(self, X):
        """Calculate predictions for specified inputs.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_inputs)
            The input samples as real numbers.

        Returns
        -------
        y : array, shape (n_samples, n_outputs)
            The predicted values as real numbers.
        """
        return super(MultiLayerPerceptronRegressor, self)._predict(X)



class MultiLayerPerceptronClassifier(BaseMLP, sklearn.base.ClassifierMixin):
    """Classifier compatible with sklearn that wraps PyLearn2.
    """

    def _setup(self):
        # WARNING: Unfortunately, sklearn's LabelBinarizer handles binary data
        # as a special case and encodes it very differently to multiclass cases.
        # In our case, we want to have 2D outputs when there are 2 classes, or
        # the predicted probabilities (e.g. Softmax) will be incorrect.
        # The LabelBinarizer is also implemented in a way that this cannot be
        # customized without a providing a complete rewrite, so here we patch
        # the `type_of_target` function for this to work correctly,
        import sklearn.preprocessing.label as L
        L.type_of_target = lambda _: "multiclass"

        self.label_binarizer = sklearn.preprocessing.LabelBinarizer()

    def fit(self, X, y):
        # check now for correct shapes
        assert X.shape[0] == y.shape[0],\
            "Expecting same number of input and output samples."

        # Scan training samples to find all different classes.
        self.label_binarizer.fit(y)
        yp = self.label_binarizer.transform(y)
        # Now train based on a problem transformed into regression.
        return super(MultiLayerPerceptronClassifier, self)._fit(X, yp, test=y)

    def partial_fit(self, X, y, classes=None):
        if classes is not None:
            self.label_binarizer.fit(classes)
        return self.fit(X, y)

    def predict_proba(self, X):
        """Calculate probability estimates based on these input features.

        Parameters
        ----------
        X : array-like of shape [n_samples, n_features]
            The input data as a numpy array.

        Returns
        -------
        y_prob : array-like of shape [n_samples, n_classes]
            The predicted probability of the sample for each class in the
            model, in the same order as the classes.
        """
        proba = super(MultiLayerPerceptronClassifier, self)._predict(X)

        return proba / proba.sum(1, keepdims=True)

    def predict(self, X):
        """Predict class by converting the problem to a regression problem.

        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)
            The input data.

        Returns
        -------
        y : array-like, shape (n_samples,) or (n_samples, n_classes)
            The predicted classes, or the predicted values.
        """
        y = self.predict_proba(X)
        return self.label_binarizer.inverse_transform(y, threshold=0.5)
