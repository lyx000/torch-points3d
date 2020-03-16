import os
from abc import ABC, abstractmethod
import logging
from functools import partial

import torch
import torch_geometric
from torch_geometric.transforms import Compose, FixedPoints

from src.core.data_transform import instantiate_transforms, MultiScaleTransform
from src.datasets.batch import SimpleBatch
from src.datasets.multiscale_data import MultiScaleBatch
from src.utils.enums import ConvolutionFormat
from src.utils.config import ConvolutionFormatFactory
from src.utils.colors import COLORS, colored_print
from src.models.base_model import BaseModel

# A logger for this file
log = logging.getLogger(__name__)


class BaseDataset:
    def __init__(self, dataset_opt):
        self.dataset_opt = dataset_opt

        # Default dataset path
        class_name = self.__class__.__name__.lower().replace("dataset", "")
        self._data_path = os.path.join(dataset_opt.dataroot, class_name)
        self._batch_size = None
        self.strategies = {}
        self._contains_dataset_name = False

        self.train_sampler = None
        self.test_sampler = None
        self.val_sampler = None

        self.train_dataset = None
        self.test_dataset = None
        self.val_dataset = None

        BaseDataset.set_transform(self, dataset_opt)

    @staticmethod
    def add_transform(transform_list_to_be_added, out=[]):
        """Add transforms to an existing list or not
        
        Arguments:
            transform_list_to_be_added {[list | T.Compose]} -- [Contains list of transform to be added]
            out {[type]} -- [Should be a lis]
        
        Returns:
            [list] -- [List of transforms]
        """
        if out is None:
            out = []
        if transform_list_to_be_added is not None:
            if isinstance(transform_list_to_be_added, Compose):
                out += transform_list_to_be_added.transforms
            elif isinstance(transform_list_to_be_added, list):
                out += transform_list_to_be_added
            else:
                raise Exception("transform_list_to_be_added should be provided either within a list or a Compose")
        return out

    @staticmethod
    def remove_transform(transform_in, list_transform_class):
        """Remove a transform if within list_transform_class
        
        Arguments:
            transform_in {[type]} -- [Compose | List of transform]
            list_transform_class {[type]} -- [List of transform class to be removed]
        
        Returns:
            [type] -- [description]
        """
        if isinstance(transform_in, Compose) or isinstance(transform_in, list):
            if len(list_transform_class) > 0:
                transform_out = []
                transforms = transform_in.transforms if isinstance(transform_in, Compose) else transform_in
                for t in transforms:
                    if not isinstance(t, tuple(list_transform_class)):
                        transform_out.append(t)
                transform_out = Compose(transform_out)
        else:
            transform_out = transform_in
        return transform_out

    @staticmethod
    def set_transform(obj, dataset_opt):
        """This function create and set the transform to the obj as attributes
        """
        obj.pre_transform = None
        obj.test_transform = None
        obj.train_transform = None
        obj.val_transform = None
        obj.inference_transform = None

        for key_name in dataset_opt.keys():
            if "transform" in key_name:
                new_name = key_name.replace("transforms", "transform")
                try:
                    transform = instantiate_transforms(getattr(dataset_opt, key_name))
                except Exception:
                    log.exception("Error trying to create {}, {}".format(new_name, getattr(dataset_opt, key_name)))
                    continue
                setattr(obj, new_name, transform)

        inference_transform = BaseDataset.add_transform(obj.pre_transform)
        inference_transform = BaseDataset.add_transform(obj.test_transform, out=inference_transform)
        obj.inference_transform = Compose(inference_transform) if len(inference_transform) > 0 else None

    @staticmethod
    def _get_collate_function(conv_type, is_multiscale):

        is_dense = ConvolutionFormatFactory.check_is_dense_format(conv_type)

        if is_multiscale:
            if conv_type.lower() == ConvolutionFormat.PARTIAL_DENSE.value.lower():
                return lambda datalist: MultiScaleBatch.from_data_list(datalist)
            else:
                raise NotImplementedError(
                    "MultiscaleTransform is activated and supported only for partial_dense format"
                )

        if is_dense:
            return lambda datalist: SimpleBatch.from_data_list(datalist)
        else:
            return lambda datalist: torch_geometric.data.batch.Batch.from_data_list(datalist)

    @staticmethod
    def get_num_samples(batch, conv_type):

        is_dense = ConvolutionFormatFactory.check_is_dense_format(conv_type)

        if is_dense:
            return batch.pos.shape[0]
        else:
            return batch.batch.max() + 1

    @staticmethod
    def get_sample(batch, key, index, conv_type):

        assert hasattr(batch, key)
        is_dense = ConvolutionFormatFactory.check_is_dense_format(conv_type)

        if is_dense:
            return batch[key][index]
        else:
            return batch[key][batch.batch == index]

    def create_dataloaders(
        self, model: BaseModel, batch_size: int, shuffle: bool, num_workers: int, precompute_multi_scale: bool,
    ):
        """ Creates the data loaders. Must be called in order to complete the setup of the Dataset
        """
        conv_type = model.conv_type
        self._batch_size = batch_size
        batch_collate_function = BaseDataset._get_collate_function(conv_type, precompute_multi_scale)
        dataloader = partial(torch.utils.data.DataLoader, collate_fn=batch_collate_function)

        if self.train_sampler:
            log.info(self.train_sampler)

        if self.train_dataset:
            self._train_loader = dataloader(
                self.train_dataset,
                batch_size=batch_size,
                shuffle=shuffle and not self.train_sampler,
                num_workers=num_workers,
                sampler=self.train_sampler,
            )

        if self.test_dataset:
            if not isinstance(self.test_dataset, list):
                test_dataset = [self.test_dataset]
            else:
                test_dataset = self.test_dataset
            self._test_loaders = [
                dataloader(
                    dataset, batch_size=batch_size, shuffle=False, num_workers=num_workers, sampler=self.test_sampler,
                )
                for dataset in test_dataset
            ]
            self._test_dataset_names = [self.get_test_dataset_name(idx) for idx in range(self.num_test_datasets)]

        if self.val_dataset:
            self._val_loader = dataloader(
                self.val_dataset,
                batch_size=batch_size,
                shuffle=False,
                num_workers=num_workers,
                sampler=self.val_sampler,
            )

        if precompute_multi_scale:
            self.set_strategies(model)

    @property
    def has_val_loader(self):
        try:
            _ = getattr(self, "_val_loader")
            return True
        except:
            False

    def val_dataloader(self):
        return self._val_loader

    def test_dataloaders(self):
        return self._test_loaders

    @property
    def num_test_datasets(self):
        return len(self._test_loaders)

    @property
    def test_datatset_names(self):
        return self._test_dataset_names

    @property
    def available_stage_names(self):
        out = self._test_dataset_names
        if self.has_val_loader:
            out += ["val"]
        return out

    @property
    def has_fixed_points_transform(self):
        """
        This property checks if the dataset contains T.FixedPoints transform, meaning the number of points is fixed
        """
        transform_train = self.train_transform
        transform_test = self.test_transform

        if transform_train is None or transform_test is None:
            return False

        if not isinstance(transform_train, Compose):
            transform_train = Compose([transform_train])

        if not isinstance(transform_test, Compose):
            transform_test = Compose([transform_test])

        train_bool = False
        test_bool = False

        for transform in transform_train.transforms:
            if isinstance(transform, FixedPoints):
                train_bool = True
        for transform in transform_test.transforms:
            if isinstance(transform, FixedPoints):
                test_bool = True
        return train_bool and test_bool

    def train_dataloader(self):
        return self._train_loader

    @property
    def is_hierarchical(self):
        """ Used by the metric trackers to log hierarchical metrics
        """
        return False

    @property
    def class_to_segments(self):
        """ Use this property to return the hierarchical map between classes and segment ids, example:
        {
            'Airplaine': [0,1,2],
            'Boat': [3,4,5]
        } 
        """
        return None

    @property
    def num_classes(self):
        return self.train_dataset.num_classes

    @property
    def weight_classes(self):
        return getattr(self.train_dataset, "weight_classes", None)

    @property
    def feature_dimension(self):
        if self.train_dataset:
            return self.train_dataset.num_features
        elif self.test_dataset is not None:
            if isinstance(self.test_dataset, list):
                return self.test_dataset[0].num_features
            else:
                return self.test_dataset.num_features
        elif self.val_dataset is not None:
            return self.val_dataset.num_features
        else:
            raise NotImplementedError()

    @property
    def batch_size(self):
        return self._batch_size

    def get_test_dataset_name(self, index=None):
        loader = self._test_loaders[index]
        if hasattr(loader.dataset, "dataset_name"):
            self._contains_dataset_name = True
            return loader.dataset.dataset_name
        else:
            if self.num_test_datasets > 1:
                return "test_{}".format(index)
            else:
                return "test"

    @property
    def num_batches(self):
        out = {
            "train": len(self._train_loader),
            "val": len(self._val_loader) if self.has_val_loader else 0,
        }
        for loader_idx, loader in enumerate(self._test_loaders):
            stage_name = self.get_test_dataset_name(loader_idx)
            out[stage_name] = len(loader)
        return out

    def _set_composed_multiscale_transform(self, attr, transform):
        current_transform = getattr(attr.dataset, "transform", None)
        if current_transform is None:
            setattr(attr.dataset, "transform", transform)
        else:
            if isinstance(current_transform, Compose):  # The transform contains several transformations
                current_transform.transforms += [transform]
            else:
                setattr(
                    attr.dataset, "transform", Compose([current_transform, transform]),
                )

    def _set_multiscale_transform(self, transform):
        for _, attr in self.__dict__.items():
            if isinstance(attr, torch.utils.data.DataLoader):
                self._set_composed_multiscale_transform(attr, transform)

        for loader in self._test_loaders:
            self._set_composed_multiscale_transform(loader, transform)

    def set_strategies(self, model):
        strategies = model.get_spatial_ops()
        transform = MultiScaleTransform(strategies)
        self._set_multiscale_transform(transform)

    @staticmethod
    @abstractmethod
    def get_tracker(model, dataset, wandb_log: bool, tensorboard_log: bool):
        pass

    def resolve_saving_stage(self, selection_stage):
        """This function is responsible to determine if the best model selection 
        is going to be on the validation or test datasets
        """
        log.info(
            "Available stage selection datasets: {} {} {}".format(
                COLORS.IPurple, self.available_stage_names, COLORS.END_NO_TOKEN
            )
        )

        if self.num_test_datasets > 1 and not self._contains_dataset_name:
            msg = "If you want to have better trackable names for your test datasets, add a "
            msg += COLORS.IPurple + "dataset_name" + COLORS.END_NO_TOKEN
            msg += " attribute to them"
            log.info(msg)

        if selection_stage == "":
            if self.has_val_loader:
                selection_stage = "val"
            else:
                selection_stage = self.get_test_dataset_name(0)
        log.info(
            "The models will be selected using the metrics on following dataset: {} {} {}".format(
                COLORS.IPurple, selection_stage, COLORS.END_NO_TOKEN
            )
        )
        return selection_stage

    def __repr__(self):
        message = "Dataset: %s \n" % self.__class__.__name__
        for attr in self.__dict__:
            if "transform" in attr:
                message += "{}{} {}= {}\n".format(COLORS.IPurple, attr, COLORS.END_NO_TOKEN, getattr(self, attr))
        for attr in self.__dict__:
            if attr.endswith("_dataset"):
                dataset = getattr(self, attr)
                if isinstance(dataset, list):
                    if len(dataset) > 1:
                        size = ", ".join([str(len(d)) for d in dataset])
                    else:
                        size = len(dataset[0])
                elif dataset:
                    size = len(dataset)
                else:
                    size = 0
                message += "Size of {}{} {}= {}\n".format(COLORS.IPurple, attr, COLORS.END_NO_TOKEN, size)
        for key, attr in self.__dict__.items():
            if key.endswith("_sampler") and attr:
                message += "{}{} {}= {}\n".format(COLORS.IPurple, key, COLORS.END_NO_TOKEN, attr)
        message += "{}Batch size ={} {}".format(COLORS.IPurple, COLORS.END_NO_TOKEN, self.batch_size)
        return message
