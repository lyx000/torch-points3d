from abc import ABC, abstractmethod
from typing import List, Union
import math
from functools import partial
import torch
from torch_geometric.nn import fps, radius, knn


class BaseSampler(ABC):
    """If num_to_sample is provided, sample exactly
        num_to_sample points. Otherwise sample floor(pos[0] * ratio) points
    """

    def __init__(self, ratio=None, num_to_sample=None):
        if num_to_sample is not None:
            if ratio is not None:
                raise ValueError("Can only specify ratio or num_to_sample, not both")
            self._num_to_sample = num_to_sample
        else:
            self._ratio = ratio

    def __call__(self, pos, batch):
        return self.sample(pos, batch)

    def _get_num_to_sample(self, pos) -> int:
        if hasattr(self, '_num_to_sample'):
            return self._num_to_sample
        else:
            return math.floor(pos.shape[0]*self._ratio)

    def _get_ratio_to_sample(self, pos) -> float:
        if hasattr(self, '_ratio'):
            return self._ratio
        else:
            return self._num_to_sample / float(pos.shape[0])

    @abstractmethod
    def sample(self, pos, batch):
        pass


class FPSSampler(BaseSampler):
    """If num_to_sample is provided, sample exactly
        num_to_sample points. Otherwise sample floor(pos[0] * ratio) points
    """

    def sample(self, pos, batch):
        return fps(pos, batch, ratio=self._get_ratio_to_sample(pos))


class RandomSampler(BaseSampler):
    """If num_to_sample is provided, sample exactly
        num_to_sample points. Otherwise sample floor(pos[0] * ratio) points
    """

    def sample(self, pos, batch):
        idx = torch.randint(0, pos.shape[0], (self._get_num_to_sample(pos),))
        return idx


class BaseNeighbourFinder(ABC):

    def __call__(self, x, y, batch_x, batch_y):
        return self.find_neighbours(x, y, batch_x, batch_y)

    @abstractmethod
    def find_neighbours(self, x, y, batch_x, batch_y):
        pass


class RadiusNeighbourFinder(BaseNeighbourFinder):

    def __init__(self, radius: float, max_num_neighbors: int = 64):
        self._radius = radius
        self._max_num_neighbors = max_num_neighbors

    def find_neighbours(self, x, y, batch_x, batch_y):
        return radius(x, y, self._radius, batch_x, batch_y, max_num_neighbors=self._max_num_neighbors)


class KNNNeighbourFinder(BaseNeighbourFinder):

    def __init__(self, k):
        self.k = k

    def find_neighbours(self, x, y, batch_x, batch_y):
        return knn(x, y, self.k, batch_x, batch_y)


class DilatedKNNNeighbourFinder(BaseNeighbourFinder):

    def __init__(self, k, dilation):
        self.k = k
        self.dilation = dilation
        self.initialFinder = KNNNeighbourFinder(k * dilation)

    def find_neighbours(self, x, y, batch_x, batch_y):
        # find the self.k * self.dilation closest neighbours in x for each y
        row, col = self.initialFinder.find_neighbours(x, y, batch_x, batch_y)

        # for each point in y, randomly select k of its neighbours
        index = torch.randint(self.k * self.dilation, (len(y), self.k), device=row.device, dtype=torch.long)

        arange = torch.arange(len(y), dtype=torch.long, device=row.device)
        arange = arange * (self.k * self.dilation)
        index = (index + arange.view(-1, 1)).view(-1)
        row, col = row[index], col[index]

        return row, col


class BaseMSNeighbourFinder(ABC):

    def __call__(self, x, y, batch_x, batch_y, scale_idx=0):
        return self.find_neighbours(x, y, batch_x, batch_y, scale_idx)

    @abstractmethod
    def find_neighbours(self, x, y, batch_x, batch_y, scale_idx=0):
        pass

    @property
    @abstractmethod
    def num_scales(self):
        pass


class MultiscaleRadiusNeighbourFinder(BaseMSNeighbourFinder):
    """ Radius search with support for multiscale

        Arguments:
            radius {Union[float, List[float]]} 

        Keyword Arguments:
            max_num_neighbors {Union[int, List[int]]}  (default: {64})

        Raises:
            ValueError: [description]
    """

    def __init__(self, radius: Union[float, List[float]], max_num_neighbors: Union[int, List[int]] = 64):
        if not isinstance(max_num_neighbors, list) and isinstance(radius, list):
            self._radius = radius
            self._max_num_neighbors = [max_num_neighbors for i in range(len(self._radius))]
            return

        if not isinstance(radius, list) and isinstance(max_num_neighbors, list):
            self._max_num_neighbors = max_num_neighbors
            self._radius = [radius for i in range(len(self._max_num_neighbors))]
            return

        if isinstance(max_num_neighbors, list):
            if len(max_num_neighbors) != len(radius):
                raise ValueError("Both lists max_num_neighbors and radius should be of the same length")
            self._max_num_neighbors = max_num_neighbors
            self._radius = radius
            return

        self._max_num_neighbors = [max_num_neighbors]
        self._radius = [radius]

    def find_neighbours(self, x, y, batch_x, batch_y, scale_idx=0):
        if scale_idx >= self.num_scales:
            raise ValueError("Scale %i is out of bounds %i" % (scale_idx, self.num_scales))
        return radius(x, y, self._radius[scale_idx], batch_x, batch_y, max_num_neighbors=self._max_num_neighbors[scale_idx])

    @property
    def num_scales(self):
        return len(self._radius)
