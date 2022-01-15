#!/usr/bin/env python

__author__ = 'Florian Hase, Matteo Aldeghi'

import numpy as np
import multiprocessing
from multiprocessing import Manager, Process
from gryffin.utilities import Logger, parse_time
import time
from contextlib import nullcontext


class SampleSelector(Logger):

    def __init__(self, config, all_options=None):
        self.config = config
        self.all_options = all_options
        self.verbosity = self.config.get('verbosity')
        Logger.__init__(self, 'SampleSelector', verbosity=self.verbosity)
        # figure out how many CPUs to use
        if self.config.get('num_cpus') == 'all':
            self.num_cpus = multiprocessing.cpu_count()
        else:
            self.num_cpus = int(self.config.get('num_cpus'))

    @staticmethod
    def compute_exp_objs(proposals, eval_acquisition, sampling_param_idx, return_index=0, return_dict=None):
        # batch_index is the index of the sampling_param_values used
        samples = proposals[sampling_param_idx]
        exp_objs = np.empty(len(samples))

        for sample_index, sample in enumerate(samples):
            acq = eval_acquisition(sample, sampling_param_idx)  # this is a method of the Acquisition instance
            exp_objs[sample_index] = np.exp(-acq)

        if return_dict.__class__.__name__ == 'DictProxy':
            return_dict[return_index] = exp_objs

        return exp_objs

    def select(self, num_batches, proposals, eval_acquisition, sampling_param_values, obs_params):
        """
        num_samples : int
            number of samples to select per sampling strategy (i.e. the ``batches`` argument in the configuration)
        proposals : ndarray
            shape of proposals is (num strategies, num samples, num dimensions).
        """
        start = time.time()
        if self.verbosity > 3.5:  # i.e. INFO or DEBUG
            cm = self.console.status("Selecting best samples to recommend...")
        else:
            cm = nullcontext()
        with cm:
            samples = self._select(num_batches, proposals, eval_acquisition, sampling_param_values, obs_params)

        # check to see if we have duplicates within in the batch
        #print('SAMPLES : ', samples)

        # NOTE: this is just a patch for the dyeopt project

        if len(self.all_options)<len(sampling_param_values):
            #pprint('HERE!!')
            # accept duplicates on the final batch, this is fine for now
            is_batch_duplicate = False
            unique_samples = samples
        else:

            unique_samples = np.unique(samples, axis=0)
            is_batch_duplicate = unique_samples.shape[0]!=sampling_param_values.shape[0]


        # print('IS BATCH DUPLICATE : ', is_batch_duplicate)
        # print('LEN UNIQUE SAMPLES : ', len(unique_samples))

        # if we have fully categorical space, remove the unique samples from the full option list
        if np.all([p['type']=='categorical' for p in self.config.parameters]):
            for sample in unique_samples:
                sample_ix = np.where(np.all(self.all_options==sample, axis=1))[0]
                self.all_options = np.delete(self.all_options, sample_ix, axis=0)

        if is_batch_duplicate:
            # get the missing samples from a random sample of the remaining options
            missing_size = sampling_param_values.shape[0] - unique_samples.shape[0]
            indices = np.random.choice(self.all_options.shape[0], size=missing_size, replace=False)
            missing_samples = self.all_options[indices, :]

            # remove new samples from all options
            for sample in missing_samples:
                sample_ix = np.where(np.all(self.all_options==sample, axis=1))[0]
                self.all_options = np.delete(self.all_options, sample_ix, axis=0)

            samples = np.concatenate((unique_samples, missing_samples), axis=0)
        else:
            samples = unique_samples

        # print('POST SAMPLES : ', samples)
        # print('LEN POST SAMPLES : ', len(samples))
        # print('LEN ALL OPTIONS : ', len(self.all_options))

        end = time.time()
        time_string = parse_time(start, end)
        samples_str = 'samples' if len(samples) > 1 else 'sample'
        self.log(f'{len(samples)} {samples_str} selected in {time_string}', 'STATS')

        return samples

    def _compute_exp_objs(self, proposals, eval_acquisition, sampling_param_values):

        exp_objs = []
        # -----------------------------------------
        # compute exponential of acquisition values
        # -----------------------------------------
        # TODO: this is slightly redundant as we have computed acquisition values already in Acquisition
        for sampling_param_idx, sampling_param in enumerate(sampling_param_values):
            # -------------------
            # parallel processing
            # -------------------
            if self.num_cpus > 1:
                return_dict = Manager().dict()
                # split proposals into approx equal chunks based on how many CPUs we're using
                proposals_splits = np.array_split(proposals, self.num_cpus, axis=1)
                # parallelize over splits
                # -----------------------
                processes = []
                for return_idx, proposals_split in enumerate(proposals_splits):
                    process = Process(target=self.compute_exp_objs, args=(proposals_split, eval_acquisition,
                                                                          sampling_param_idx,
                                                                          return_idx, return_dict))
                    processes.append(process)
                    process.start()
                # wait until all processes finished
                for process in processes:
                    process.join()
                # sort results in return_dict to create batch_exp_objs list with correct sample order
                batch_exp_objs = []
                for idx in range(len(proposals_splits)):
                    batch_exp_objs.extend(return_dict[idx])
            # ---------------------
            # sequential processing
            # ---------------------
            else:
                batch_exp_objs = self.compute_exp_objs(proposals=proposals, eval_acquisition=eval_acquisition,
                                                       sampling_param_idx=sampling_param_idx, return_index=0,
                                                       return_dict=None)
            # append the proposed samples for this sampling strategy to the global list of samples
            exp_objs.append(batch_exp_objs)
        # cast to np.array
        exp_objs = np.array(exp_objs)

        return exp_objs


    def _select(self, num_batches, proposals, eval_acquisition, sampling_param_values, obs_params):
        num_obs = len(obs_params)
        feature_ranges = self.config.feature_ranges
        char_dists = feature_ranges / float(num_obs)**0.5

        exp_objs = self._compute_exp_objs(proposals, eval_acquisition, sampling_param_values)

        # ----------------------------------------
        # compute prior recommendation punishments
        # ----------------------------------------

        # compute normalised obs_params. In this way, we can rely on normalized distance thresholds, otherwise
        # if obs_params has very small range, sample selector is messed up
        obs_params_norm = (obs_params - self.config.param_lowers) / (self.config.param_uppers - self.config.param_lowers)
        proposals_norm = (proposals - self.config.param_lowers) / (self.config.param_uppers - self.config.param_lowers)

        # here we set to zero the reward if proposals are too close to previous observed params
        for sampling_param_idx in range(len(sampling_param_values)):
            batch_proposals = proposals_norm[sampling_param_idx, : exp_objs.shape[1]]


            # compute distance to each obs_param
            distances = [np.sum((obs_params_norm - batch_proposal)**2, axis=1) for batch_proposal in batch_proposals]
            distances = np.array(distances)
            # take min distance across dimensions
            min_distances = np.amin(distances, axis=1)
            # get indices for proposals that are basically the same as previous samples
            ident_indices = np.where(min_distances < 1e-8)[0]
            # set reward to zero for these samples since we do not want to select them
            exp_objs[sampling_param_idx, ident_indices] = 0.

        # ---------------
        # collect samples
        # ---------------
        # here we add a penalty term that depends on the minimum distance between proposals and previous observations,
        # or other samples that have been selected.
        selected_samples = []
        for batch_idx in range(num_batches):
            for sampling_param_idx in range(len(sampling_param_values)):
                # proposals.shape = (# sampling params, # proposals, # param dims)
                batch_proposals = proposals[sampling_param_idx, :, :]

                # compute diversity punishments
                num_proposals_in_batch = exp_objs.shape[1]
                div_crits = np.ones(num_proposals_in_batch)  # exp_objs shape = (# sampling params, # proposals)

                # iterate over batch proposals and compute min distance to previous observations
                # or other proposed samples
                for proposal_index, proposal in enumerate(batch_proposals):
                    # compute min distance to observed samples
                    obs_min_distance = np.amin([np.abs(proposal - x) for x in obs_params], axis=0)
                    # if we already chose a new sample, compute also min distance to newly chosen samples
                    if len(selected_samples) > 0:
                        min_distance = np.amin([np.abs(proposal - x) for x in selected_samples], axis=0)
                        min_distance = np.minimum(min_distance, obs_min_distance)
                    else:
                        min_distance = obs_min_distance

                    # compute distance reward
                    div_crits[proposal_index] = np.minimum(1., np.mean(np.exp(2. * (min_distance - char_dists) / feature_ranges)))

                # reweight computed based on acquisition with rewards based on distance
                reweighted_rewards = exp_objs[sampling_param_idx] * div_crits
                # get index of proposal with largest rewards
                largest_reward_index = np.argmax(reweighted_rewards)

                # select the sample from batch_proposals
                # not from batch_proposals_norm that was used only for computing penalties
                new_sample = batch_proposals[largest_reward_index]
                selected_samples.append(new_sample)

                # update reward of selected sample
                exp_objs[sampling_param_idx, largest_reward_index] = 0.

        # TODO: check to see if we have duplicated samples. If we do, replace them with
        # random samples from the remaining options

        return np.array(selected_samples)
