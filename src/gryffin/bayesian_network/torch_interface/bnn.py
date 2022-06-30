from typing import OrderedDict
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torch.distributions as td
import torchbnn as bnn

from gryffin.utilities import Logger, GryffinUnknownSettingsError, GryffinComputeError
from ..tfprob_interface.numpy_graph import NumpyGraph


class BNNTrainer(Logger):

    def __init__(self, config, model_details, frac_feas):
        
        self.config = config
        self.model_details = model_details
        self.frac_feas = frac_feas

        Logger.__init__(self, 'BNNTrainer', verbosity=self.config.get('verbosity'))

    def train(self, observed_params):
        observed_params = torch.tensor(observed_params)
        features, targets = self._generate_train_data(observed_params)
        num_observations = len(observed_params)

        model = self._construct_model(num_observations)
        model.register_numpy_graph(features)
        optimizer = optim.Adam(model.parameters(), lr=self.model_details['learning_rate'])
        kl_loss = bnn.BKLLoss(reduction='mean', last_layer_only=False)

        # tmp until added to real config
        self.model_details['kl_weight'] = 0.01



        for _ in range(self.model_details['num_epochs']):
            inferences = model(features, targets)
            loss = 0.0
            for inference in inferences:
                loss = loss - torch.sum(inference['pred'].log_prob(inference['target']))
                loss = loss +  self.model_details['kl_weight'] * kl_loss(model)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        return model

    def _construct_model(self, num_observations):
        return BNN(self.config, self.model_details, num_observations, self.frac_feas)

    def _generate_train_data(self, observed_params):
        
        feature_size = len(self.config.kernel_names)
        bnn_output_size = len(self.config.kernel_names) 
        target_size = len(self.config.kernel_names)
        num_obs = len(observed_params)

        # initialize training features and targets
        features = torch.zeros((num_obs, feature_size))
        targets = torch.zeros((num_obs, target_size))

        # construct training features
        feature_begin = 0
        feature_sizes = self.config.feature_sizes
        for feature_index, feature_type in enumerate(self.config.feature_types):
            feature_size = feature_sizes[feature_index]
            if feature_type == 'categorical':
                for obs_param_index, obs_param in enumerate(observed_params):
                    features[obs_param_index, feature_begin + int(obs_param[feature_index])] += 1
            elif feature_type == 'discrete':
                for obs_param_index, obs_param in enumerate(observed_params):
                    features[obs_param_index, feature_begin + int(obs_param[feature_index])] += 1
            elif feature_type == 'continuous':
                features[:, feature_begin] = observed_params[:, feature_index]
            else:
                raise NotImplementedError
            feature_begin += feature_size
        targets = features.clone() ## Do I need to break graph here?

        # rescale features
        lower_rescalings = torch.empty(feature_size)
        upper_rescalings = torch.empty(feature_size)
        kernel_uppers, kernel_lowers = self.config.kernel_uppers, self.config.kernel_lowers
        for kernel_index, kernel_name in enumerate(self.config.kernel_names):
            low = kernel_lowers[kernel_index]
            up  = kernel_uppers[kernel_index]
            lower_rescalings[kernel_index] = low  # - 0.1 * (up - low)
            upper_rescalings[kernel_index] = up   # + 0.1 * (up - low)

        lower_rescalings = lower_rescalings
        upper_rescalings = upper_rescalings

        rescaled_features = (features - lower_rescalings) / (upper_rescalings - lower_rescalings)
        rescaled_targets = (targets - lower_rescalings) / (upper_rescalings - lower_rescalings)
        return (rescaled_features, rescaled_targets)


class BNN(nn.Module):

    def __init__(self, config, model_details, num_observations, frac_feas):
        super(BNN, self).__init__() 

        print(model_details)
        self.num_draws = model_details['num_draws']
        self.hidden_shape = model_details['hidden_shape']
        self.feature_size = len(config.kernel_names)
        self.bnn_output_size = len(config.kernel_names)
        self.num_obs = num_observations
        self.frac_feas = frac_feas

        self.numpy_graph = NumpyGraph(config, model_details)

        self.kernel_names = config.kernel_names
        self.kernel_types = config.kernel_types
        self.kernel_ranges = torch.tensor(config.kernel_ranges)
        self.kernel_uppers = torch.tensor(config.kernel_uppers)
        self.kernel_lowers = torch.tensor(config.kernel_lowers)
        self.kernel_sizes = torch.tensor(config.kernel_sizes)
        
        self.layers = nn.Sequential(OrderedDict([
            ('linear1', bnn.BayesLinear(prior_mu=0.0, prior_sigma=1.0, in_features=self.feature_size, out_features=self.hidden_shape, bias=True)),
            ('relu1', nn.ReLU()),
            ('linear2', bnn.BayesLinear(prior_mu=0.0, prior_sigma=1.0, in_features=self.hidden_shape, out_features=self.hidden_shape, bias=True)),
            ('relu2', nn.ReLU()),
            ('linear3', bnn.BayesLinear(prior_mu=0.0, prior_sigma=1.0, in_features=self.hidden_shape, out_features=self.bnn_output_size, bias=True)),
        ]))
        
        self.tau_rescaling = torch.zeros((self.num_obs, self.bnn_output_size))
        for obs_index in range(self.num_obs):
            self.tau_rescaling[obs_index] += self.kernel_ranges
        self.tau_rescaling = self.tau_rescaling**2
        self.gamma_concentration = nn.Parameter(torch.zeros(self.num_obs, self.bnn_output_size) + 12*(self.num_obs/self.frac_feas)**2)
        self.gamma_rate = nn.Parameter(F.softplus(torch.ones(self.num_obs, self.bnn_output_size)))
        
        self.tau_normed = td.gamma.Gamma(self.gamma_concentration, self.gamma_rate)

    def forward(self, x, y):

        x = self.layers(x)
        
        scale = 1.0 / torch.sqrt(self.tau_normed.sample() / self.tau_rescaling)
        
        inferences = []
        kernel_element_index = 0
        target_element_index = 0
        while kernel_element_index < len(self.kernel_names):

                kernel_type = self.kernel_types[kernel_element_index]
                kernel_size = self.kernel_sizes[kernel_element_index]

                feature_begin, feature_end = target_element_index, target_element_index + 1
                kernel_begin, kernel_end   = kernel_element_index, kernel_element_index + kernel_size

                post_relevant  = x[:,  kernel_begin: kernel_end]
                
                target = y[:, kernel_begin: kernel_end]
                lowers, uppers = self.kernel_lowers[kernel_begin: kernel_end], self.kernel_uppers[kernel_begin : kernel_end]

                post_support = (uppers - lowers) * (1.2 * F.sigmoid(post_relevant) - 0.1) + lowers

                post_predict = td.normal.Normal(post_support,  scale[:,  kernel_begin: kernel_end])

            
                inference = {'pred': post_predict, 'target': target}
                inferences.append(inference)
                
                kernel_element_index += kernel_size
                target_element_index += 1
                
        return inferences

    def register_numpy_graph(self, features):
        self.numpy_graph.declare_training_data(features)

    def _sample(self, num_draws):

        print('here')
        posterior_samples = {}

        idx = 0
        for name, module in self.layers.named_modules():
            print(module)
            if isinstance(module, bnn.BayesLinear):
                print('here2')
                # weight_dist = td.multivariate_normal.MultivariateNormal(module.weight_mu, torch.exp(module.weight_log_sigma))
                weight_dist = td.normal.Normal(module.weight_mu, torch.exp(module.weight_log_sigma))
                bias_dist = td.normal.Normal(module.bias_mu, torch.exp(module.bias_log_sigma))
                # print(weight_dist)
                # print(weight_dist.sample())
                # #print(weight_dist.sample(sample_shape=(num_draws, module.weight_mu.shape[0])))
                # sample_test = weight_dist.sample(sample_shape=(num_draws, 1))
                # print(sample_test.shape)
                # print(sample_test[0])
                #import pdb; pdb.set_trace()
                weight_sample = weight_dist.sample(sample_shape=(num_draws, 1)).squeeze()
                if idx == 0:
                    weight_sample = weight_sample.unsqueeze(1)
                elif idx == 2:
                    weight_sample = weight_sample.unsqueeze(-1)
                import pdb; pdb.set_trace()
                bias_sample = bias_dist.sample(sample_shape=(num_draws, 1)).squeeze()

                posterior_samples['weight_%d' % idx] = weight_sample.numpy()
                posterior_samples['bias_%d' % idx] = bias_sample.numpy()
                idx += 1

        print(posterior_samples)
        posterior_samples['gamma'] = self.tau_normed.sample(sample_shape=(num_draws, 1))
        print('here3')
        print(posterior_samples.keys())
        post_kernels = self.numpy_graph.compute_kernels(posterior_samples, self.frac_feas)

        self.trace = {}
        for key in post_kernels.keys():
            self.trace[key] = {}
            kernel_dict = post_kernels[key]
            for kernel_name, kernel_values in kernel_dict.items():
                self.trace[key][kernel_name] = kernel_values

    def get_kernels(self, num_draws=None):

        if num_draws == None:
            num_draws = self.num_draws
        self._sample(num_draws)
        
        print(self.trace)

        trace_kernels = {'locs': [], 'sqrt_precs': [], 'probs': []}
        for param_index in range(len(self.config['param_names'])):
            post_kernel = self.trace['param_%d' % param_index]

            # ------------------
            # continuous kernels
            # ------------------
            if 'loc' in post_kernel and 'sqrt_prec' in post_kernel:
                trace_kernels['locs'].append(post_kernel['loc'].astype(torch.float64))
                trace_kernels['sqrt_precs'].append(post_kernel['sqrt_prec'].astype(torch.float64))
                # for continuous variables, key "probs" contains all zeros
                trace_kernels['probs'].append(torch.zeros(post_kernel['loc'].shape, dtype=torch.float64))

            # ------------------
            # categorical kernels
            # ------------------
            elif 'probs' in post_kernel:
                # for categorical variables, keys "locs" and "precs" are all zeros
                trace_kernels['locs'].append(np.zeros(post_kernel['probs'].shape, dtype=np.float64))
                trace_kernels['sqrt_precs'].append(np.zeros(post_kernel['probs'].shape, dtype=np.float64))
                trace_kernels['probs'].append(post_kernel['probs'].astype(np.float64))
            else:
                raise NotImplementedError

        for key, kernel in trace_kernels.items():
            trace_kernels[key] = np.concatenate(kernel, axis=2)

        return trace_kernels





