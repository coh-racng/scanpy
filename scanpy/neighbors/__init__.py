from typing import Union, Optional, Any, Mapping, Callable, NamedTuple, Generator, Tuple

import numpy as np
import scipy
from anndata import AnnData
from numpy.random import RandomState
from scipy.sparse import issparse, coo_matrix, csr_matrix
from sklearn.utils import check_random_state

from .. import logging as logg
from .. import utils
from ..utils import doc_params
from ..tools._utils import choose_representation, doc_use_rep, doc_n_pcs

N_DCS = 15  # default number of diffusion components
N_PCS = 50  # default number of PCs


Metric = Callable[[np.ndarray, np.ndarray], float]


@doc_params(n_pcs=doc_n_pcs, use_rep=doc_use_rep)
def neighbors(
    adata: AnnData,
    n_neighbors: int = 15,
    n_pcs: Optional[int] = None,
    use_rep: Optional[str] = None,
    knn: bool = True,
    random_state: Optional[Union[int, RandomState]] = 0,
    method: str = 'umap',
    metric: Union[str, Metric] = 'euclidean',
    metric_kwds: Mapping[str, Any] = {},
    copy: bool = False
) -> Optional[AnnData]:
    """\
    Compute a neighborhood graph of observations [McInnes18]_.

    The neighbor search efficiency of this heavily relies on UMAP [McInnes18]_,
    which also provides a method for estimating connectivities of data points -
    the connectivity of the manifold (`method=='umap'`). If `method=='gauss'`,
    connectivities are computed according to [Coifman05]_, in the adaption of
    [Haghverdi16]_.

    Parameters
    ----------
    adata
        Annotated data matrix.
    n_neighbors
        The size of local neighborhood (in terms of number of neighboring data
        points) used for manifold approximation. Larger values result in more
        global views of the manifold, while smaller values result in more local
        data being preserved. In general values should be in the range 2 to 100.
        If `knn` is `True`, number of nearest neighbors to be searched. If `knn`
        is `False`, a Gaussian kernel width is set to the distance of the
        `n_neighbors` neighbor.
    {n_pcs}
    {use_rep}
    knn
        If `True`, use a hard threshold to restrict the number of neighbors to
        `n_neighbors`, that is, consider a knn graph. Otherwise, use a Gaussian
        Kernel to assign low weights to neighbors more distant than the
        `n_neighbors` nearest neighbor.
    random_state
        A numpy random seed.
    method : {{'umap', 'gauss', `None`}}  (default: `'umap'`)
        Use 'umap' [McInnes18]_ or 'gauss' (Gauss kernel following [Coifman05]_
        with adaptive width [Haghverdi16]_) for computing connectivities.
    metric
        A known metric’s name or a callable that returns a distance.
    metric_kwds
        Options for the metric.
    copy
        Return a copy instead of writing to adata.

    Returns
    -------
    Depending on `copy`, updates or returns `adata` with the following:

    **connectivities** : sparse matrix (`.uns['neighbors']`, dtype `float32`)
        Weighted adjacency matrix of the neighborhood graph of data
        points. Weights should be interpreted as connectivities.
    **distances** : sparse matrix (`.uns['neighbors']`, dtype `float32`)
        Instead of decaying weights, this stores distances for each pair of
        neighbors.
    """
    start = logg.info('computing neighbors')
    adata = adata.copy() if copy else adata
    if adata.isview:  # we shouldn't need this here...
        adata._init_as_actual(adata.copy())
    neighbors = Neighbors(adata)
    neighbors.compute_neighbors(
        n_neighbors=n_neighbors, knn=knn, n_pcs=n_pcs, use_rep=use_rep,
        method=method, metric=metric, metric_kwds=metric_kwds,
        random_state=random_state,
    )
    adata.uns['neighbors'] = {}
    adata.uns['neighbors']['params'] = {'n_neighbors': n_neighbors, 'method': method}
    adata.uns['neighbors']['params']['metric'] = metric
    if metric_kwds:
        adata.uns['neighbors']['params']['metric_kwds'] = metric_kwds
    if use_rep is not None:
        adata.uns['neighbors']['params']['use_rep'] = use_rep
    if n_pcs is not None:
        adata.uns['neighbors']['params']['n_pcs'] = n_pcs
    adata.uns['neighbors']['distances'] = neighbors.distances
    adata.uns['neighbors']['connectivities'] = neighbors.connectivities
    if neighbors.rp_forest is not None:
        adata.uns['neighbors']['rp_forest'] = neighbors.rp_forest
    logg.info(
        '    finished',
        time=start,
        deep=(
            'added to `.uns[\'neighbors\']`\n'
            '    \'distances\', distances for each pair of neighbors\n'
            '    \'connectivities\', weighted adjacency matrix'
        ),
    )
    return adata if copy else None


class FlatTree(NamedTuple):
    hyperplanes: None
    offsets: None
    children: None
    indices: None


RPForestDict = Mapping[str, Mapping[str, np.ndarray]]


def _rp_forest_generate(rp_forest_dict: RPForestDict) -> Generator[FlatTree, None, None]:
    props = FlatTree._fields[0]
    num_trees = len(rp_forest_dict[props]['start'])-1

    for i in range(num_trees):
        tree = []
        for prop in props:
            start = rp_forest_dict[prop]['start'][i]
            end = rp_forest_dict[prop]['start'][i+1]
            tree.append(rp_forest_dict[prop]['data'][start:end])
        yield FlatTree(*tree)

    tree = []
    for prop in props:
        start = rp_forest_dict[prop]['start'][num_trees]
        tree.append(rp_forest_dict[prop]['data'][start:])
    yield FlatTree(*tree)


def neighbors_update(adata, adata_new, k=10, queue_size=5, random_state=0):
    # only with use_rep='X' for now
    from umap.nndescent import make_initialisations, make_initialized_nnd_search, initialise_search
    from umap.umap_ import INT32_MAX, INT32_MIN
    from umap.utils import deheap_sort
    import umap.distances as dist

    if 'metric_kwds' in adata.uns['neighbors']['params']:
        dist_args = tuple(adata.uns['neighbors']['params']['metric_kwds'].values())
    else:
        dist_args = ()
    dist_func = dist.named_distances[adata.uns['neighbors']['params']['metric']]

    random_init, tree_init = make_initialisations(dist_func, dist_args)
    search = make_initialized_nnd_search(dist_func, dist_args)

    search_graph = adata.uns['neighbors']['distances'].copy()
    search_graph.data = (search_graph.data > 0).astype(np.int8)
    search_graph = search_graph.maximum(search_graph.transpose())
    # prune it?

    random_state = check_random_state(random_state)
    rng_state = random_state.randint(INT32_MIN, INT32_MAX, 3).astype(np.int64)

    if 'rp_forest' in adata.uns['neighbors']:
        rp_forest = _rp_forest_generate(adata.uns['neighbors']['rp_forest'])
    else:
        rp_forest = None
    train = adata.X
    test = adata_new.X

    init = initialise_search(rp_forest, train, test, int(k * queue_size), random_init, tree_init, rng_state)
    result = search(train, search_graph.indptr, search_graph.indices, init, test)

    indices, dists = deheap_sort(result)
    return indices[:, :k], dists[:, :k]


def compute_neighbors_umap(
    X: Union[np.ndarray, csr_matrix],
    n_neighbors: int,
    random_state: Optional[Union[int, RandomState]] = None,
    metric: Union[str, Metric] = 'euclidean',
    metric_kwds: Mapping[str, Any] = {},
    angular: bool = False,
    verbose: bool = False,
):
    """This is from umap.fuzzy_simplicial_set [McInnes18]_.

    Given a set of data X, a neighborhood size, and a measure of distance
    compute the fuzzy simplicial set (here represented as a fuzzy graph in
    the form of a sparse matrix) associated to the data. This is done by
    locally approximating geodesic distance at each point, creating a fuzzy
    simplicial set for each such point, and then combining all the local
    fuzzy simplicial sets into a global one via a fuzzy union.

    Parameters
    ----------
    X: array of shape (n_samples, n_features)
        The data to be modelled as a fuzzy simplicial set.
    n_neighbors
        The number of neighbors to use to approximate geodesic distance.
        Larger numbers induce more global estimates of the manifold that can
        miss finer detail, while smaller values will focus on fine manifold
        structure to the detriment of the larger picture.
    random_state
        A state capable being used as a numpy random state.
    metric
        The metric to use to compute distances in high dimensional space.
        If a string is passed it must match a valid predefined metric. If
        a general metric is required a function that takes two 1d arrays and
        returns a float can be provided. For performance purposes it is
        required that this be a numba jit'd function. Valid string metrics
        include:
            * euclidean
            * manhattan
            * chebyshev
            * minkowski
            * canberra
            * braycurtis
            * mahalanobis
            * wminkowski
            * seuclidean
            * cosine
            * correlation
            * haversine
            * hamming
            * jaccard
            * dice
            * russelrao
            * kulsinski
            * rogerstanimoto
            * sokalmichener
            * sokalsneath
            * yule
        Metrics that take arguments (such as minkowski, mahalanobis etc.)
        can have arguments passed via the metric_kwds dictionary. At this
        time care must be taken and dictionary elements must be ordered
        appropriately; this will hopefully be fixed in the future.
    metric_kwds
        Arguments to pass on to the metric, such as the ``p`` value for
        Minkowski distance.
    angular
        Whether to use angular/cosine distance for the random projection
        forest for seeding NN-descent to determine approximate nearest
        neighbors.
    verbose
        Whether to report information on the current progress of the algorithm.

    Returns
    -------
    **knn_indices**, **knn_dists** : np.arrays of shape (n_observations, n_neighbors)
    """
    from umap.umap_ import nearest_neighbors

    random_state = check_random_state(random_state)

    knn_indices, knn_dists, forest = nearest_neighbors(
        X, n_neighbors, random_state=random_state,
        metric=metric, metric_kwds=metric_kwds,
        angular=angular, verbose=verbose,
    )

    return knn_indices, knn_dists, forest


def get_sparse_matrix_from_indices_distances_umap(knn_indices, knn_dists, n_obs, n_neighbors):
    rows = np.zeros((n_obs * n_neighbors), dtype=np.int64)
    cols = np.zeros((n_obs * n_neighbors), dtype=np.int64)
    vals = np.zeros((n_obs * n_neighbors), dtype=np.float64)

    for i in range(knn_indices.shape[0]):
        for j in range(n_neighbors):
            if knn_indices[i, j] == -1:
                continue  # We didn't get the full knn for i
            if knn_indices[i, j] == i:
                val = 0.0
            else:
                val = knn_dists[i, j]

            rows[i * n_neighbors + j] = i
            cols[i * n_neighbors + j] = knn_indices[i, j]
            vals[i * n_neighbors + j] = val

    result = coo_matrix((vals, (rows, cols)),
                                      shape=(n_obs, n_obs))
    result.eliminate_zeros()
    return result.tocsr()


def compute_connectivities_umap(
    knn_indices, knn_dists,
    n_obs, n_neighbors, set_op_mix_ratio=1.0,
    local_connectivity=1.0,
):
    """This is from umap.fuzzy_simplicial_set [McInnes18]_.

    Given a set of data X, a neighborhood size, and a measure of distance
    compute the fuzzy simplicial set (here represented as a fuzzy graph in
    the form of a sparse matrix) associated to the data. This is done by
    locally approximating geodesic distance at each point, creating a fuzzy
    simplicial set for each such point, and then combining all the local
    fuzzy simplicial sets into a global one via a fuzzy union.
    """
    from umap.umap_ import fuzzy_simplicial_set

    X = coo_matrix(([], ([], [])), shape=(n_obs, 1))
    connectivities = fuzzy_simplicial_set(X, n_neighbors, None, None,
                                          knn_indices=knn_indices, knn_dists=knn_dists,
                                          set_op_mix_ratio=set_op_mix_ratio,
                                          local_connectivity=local_connectivity)
    distances = get_sparse_matrix_from_indices_distances_umap(knn_indices, knn_dists, n_obs, n_neighbors)

    return distances, connectivities.tocsr()


def get_sparse_matrix_from_indices_distances_numpy(indices, distances, n_obs, n_neighbors):
    n_nonzero = n_obs * n_neighbors
    indptr = np.arange(0, n_nonzero + 1, n_neighbors)
    D = csr_matrix((
        distances.copy().ravel(),  # copy the data, otherwise strange behavior here
        indices.copy().ravel(),
        indptr,
    ), shape=(n_obs, n_obs))
    D.eliminate_zeros()
    return D


def get_indices_distances_from_sparse_matrix(D, n_neighbors: int):
    indices = np.zeros((D.shape[0], n_neighbors), dtype=int)
    distances = np.zeros((D.shape[0], n_neighbors), dtype=D.dtype)
    n_neighbors_m1 = n_neighbors - 1
    for i in range(indices.shape[0]):
        neighbors = D[i].nonzero()  # 'true' and 'spurious' zeros
        indices[i, 0] = i
        distances[i, 0] = 0
        # account for the fact that there might be more than n_neighbors
        # due to an approximate search
        # [the point itself was not detected as its own neighbor during the search]
        if len(neighbors[1]) > n_neighbors_m1:
            sorted_indices = np.argsort(D[i][neighbors].A1)[:n_neighbors_m1]
            indices[i, 1:] = neighbors[1][sorted_indices]
            distances[i, 1:] = D[i][
                neighbors[0][sorted_indices], neighbors[1][sorted_indices]]
        else:
            indices[i, 1:] = neighbors[1]
            distances[i, 1:] = D[i][neighbors]
    return indices, distances


def get_indices_distances_from_dense_matrix(D, n_neighbors: int):
    sample_range = np.arange(D.shape[0])[:, None]
    indices = np.argpartition(D, n_neighbors-1, axis=1)[:, :n_neighbors]
    indices = indices[sample_range, np.argsort(D[sample_range, indices])]
    distances = D[sample_range, indices]
    return indices, distances


def _backwards_compat_get_full_X_diffmap(adata: AnnData) -> np.ndarray:
    if 'X_diffmap0' in adata.obs:
        return np.c_[adata.obs['X_diffmap0'].values[:, None],
                     adata.obsm['X_diffmap']]
    else:
        return adata.obsm['X_diffmap']


def _backwards_compat_get_full_eval(adata: AnnData):
    if 'X_diffmap0' in adata.obs:
        return np.r_[1, adata.uns['diffmap_evals']]
    else:
        return adata.uns['diffmap_evals']


def _make_forest_dict(forest):
    d = {}
    props = ('hyperplanes', 'offsets', 'children', 'indices')
    for prop in props:
        d[prop] = {}
        sizes = np.fromiter((getattr(tree, prop).shape[0] for tree in forest), dtype=int)
        d[prop]['start'] = np.zeros_like(sizes)
        if prop == 'offsets':
            dims = sizes.sum()
        else:
            dims = (sizes.sum(), getattr(forest[0], prop).shape[1])
        dtype = getattr(forest[0], prop).dtype
        dat = np.empty(dims, dtype=dtype)
        start = 0
        for i, size in enumerate(sizes):
            d[prop]['start'][i] = start
            end = start+size
            dat[start:end] = getattr(forest[i], prop)
            start = end
        d[prop]['data'] = dat
    return d


class OnFlySymMatrix:
    """Emulate a matrix where elements are calculated on the fly.
    """
    def __init__(
        self,
        get_row: Callable[[Any], np.ndarray],
        shape: Tuple[int, int],
        DC_start: int = 0,
        DC_end: int = -1,
        rows: Optional[Mapping[Any, np.ndarray]] = None,
        restrict_array: Optional[np.ndarray] = None,
    ):
        self.get_row = get_row
        self.shape = shape
        self.DC_start = DC_start
        self.DC_end = DC_end
        self.rows = {} if rows is None else rows
        self.restrict_array = restrict_array  # restrict the array to a subset

    def __getitem__(self, index):
        if isinstance(index, (int, np.integer)):
            if self.restrict_array is None:
                glob_index = index
            else:
                # map the index back to the global index
                glob_index = self.restrict_array[index]
            if glob_index not in self.rows:
                self.rows[glob_index] = self.get_row(glob_index)
            row = self.rows[glob_index]
            if self.restrict_array is None:
                return row
            else:
                return row[self.restrict_array]
        else:
            if self.restrict_array is None:
                glob_index_0, glob_index_1 = index
            else:
                glob_index_0 = self.restrict_array[index[0]]
                glob_index_1 = self.restrict_array[index[1]]
            if glob_index_0 not in self.rows:
                self.rows[glob_index_0] = self.get_row(glob_index_0)
            return self.rows[glob_index_0][glob_index_1]

    def restrict(self, index_array):
        """Generate a view restricted to a subset of indices.
        """
        new_shape = index_array.shape[0], index_array.shape[0]
        return OnFlySymMatrix(
            self.get_row, new_shape, DC_start=self.DC_start,
            DC_end=self.DC_end,
            rows=self.rows, restrict_array=index_array,
        )


class Neighbors:
    """Data represented as graph of nearest neighbors.

    Represent a data matrix as a graph of nearest neighbor relations (edges)
    among data points (nodes).

    Parameters
    ----------
    adata
        Annotated data object.
    n_dcs
        Number of diffusion components to use.
    """

    def __init__(self, adata: AnnData, n_dcs: Optional[int] = None):
        self._adata = adata
        self._init_iroot()
        # use the graph in adata
        info_str = ''
        self.knn: Optional[bool] = None
        self._distances: Union[np.ndarray, csr_matrix, None] = None
        self._connectivities: Union[np.ndarray, csr_matrix, None] = None
        self._transitions_sym: Union[np.ndarray, csr_matrix, None] = None
        self._number_connected_components: Optional[int] = None
        self._rp_forest: Optional[RPForestDict] = None
        if 'neighbors' in adata.uns:
            if 'distances' in adata.uns['neighbors']:
                self.knn = issparse(adata.uns['neighbors']['distances'])
                self._distances = adata.uns['neighbors']['distances']
            if 'connectivities' in adata.uns['neighbors']:
                self.knn = issparse(adata.uns['neighbors']['connectivities'])
                self._connectivities = adata.uns['neighbors']['connectivities']
            if 'rp_forest' in adata.uns['neighbors']:
                self._rp_forest = adata.uns['neighbors']['rp_forest']
            if 'params' in adata.uns['neighbors']:
                self.n_neighbors = adata.uns['neighbors']['params']['n_neighbors']
            else:
                def count_nonzero(a: Union[np.ndarray, csr_matrix]) -> int:
                    return a.count_nonzero() if issparse(a) else np.count_nonzero(a)
                # estimating n_neighbors
                if self._connectivities is None:
                    self.n_neighbors = int(count_nonzero(self._distances) / self._distances.shape[0])
                else:
                    self.n_neighbors = int(count_nonzero(self._connectivities) / self._connectivities.shape[0] / 2)
            info_str += '`.distances` `.connectivities` '
            self._number_connected_components = 1
            if issparse(self._connectivities):
                from scipy.sparse.csgraph import connected_components
                self._connected_components = connected_components(self._connectivities)
                self._number_connected_components = self._connected_components[0]
        if 'X_diffmap' in adata.obsm_keys():
            self._eigen_values = _backwards_compat_get_full_eval(adata)
            self._eigen_basis = _backwards_compat_get_full_X_diffmap(adata)
            if n_dcs is not None:
                if n_dcs > len(self._eigen_values):
                    raise ValueError(
                        'Cannot instantiate using `n_dcs`={}. '
                        'Compute diffmap/spectrum with more components first.'
                        .format(n_dcs))
                self._eigen_values = self._eigen_values[:n_dcs]
                self._eigen_basis = self._eigen_basis[:, :n_dcs]
            self.n_dcs = len(self._eigen_values)
            info_str += '`.eigen_values` `.eigen_basis` `.distances_dpt`'
        else:
            self._eigen_values = None
            self._eigen_basis = None
            self.n_dcs = None
        if info_str != '':
            logg.debug(f'    initialized {info_str}')

    @property
    def rp_forest(self) -> Optional[RPForestDict]:
        return self._rp_forest

    @property
    def distances(self) -> Union[np.ndarray, csr_matrix, None]:
        """Distances between data points (sparse matrix).
        """
        return self._distances

    @property
    def connectivities(self) -> Union[np.ndarray, csr_matrix, None]:
        """Connectivities between data points (sparse matrix).
        """
        return self._connectivities

    @property
    def transitions(self) -> Union[np.ndarray, csr_matrix]:
        """Transition matrix (sparse matrix).

        Is conjugate to the symmetrized transition matrix via::

            self.transitions = self.Z *  self.transitions_sym / self.Z

        where ``self.Z`` is the diagonal matrix storing the normalization of the
        underlying kernel matrix.

        Notes
        -----
        This has not been tested, in contrast to `transitions_sym`.
        """
        if issparse(self.Z):
            Zinv = self.Z.power(-1)
        else:
            Zinv = np.diag(1./np.diag(self.Z))
        return self.Z @ self.transitions_sym @ Zinv

    @property
    def transitions_sym(self) -> Union[np.ndarray, csr_matrix, None]:
        """Symmetrized transition matrix (sparse matrix).

        Is conjugate to the transition matrix via::

            self.transitions_sym = self.Z /  self.transitions * self.Z

        where ``self.Z`` is the diagonal matrix storing the normalization of the
        underlying kernel matrix.
        """
        return self._transitions_sym

    @property
    def eigen_values(self):
        """Eigen values of transition matrix (numpy array).
        """
        return self._eigen_values

    @property
    def eigen_basis(self):
        """Eigen basis of transition matrix (numpy array).
        """
        return self._eigen_basis

    @property
    def distances_dpt(self):
        """DPT distances (on-fly matrix).

        This is yields [Haghverdi16]_, Eq. 15 from the supplement with the
        extensions of [Wolf19]_, supplement on random-walk based distance
        measures.
        """
        return OnFlySymMatrix(self._get_dpt_row, shape=self._adata.shape)

    def to_igraph(self):
        """Generate igraph from connectiviies.
        """
        return utils.get_igraph_from_adjacency(self.connectivities)

    @doc_params(n_pcs=doc_n_pcs, use_rep=doc_use_rep)
    def compute_neighbors(
        self,
        n_neighbors: int = 30,
        knn: bool = True,
        n_pcs: Optional[int] = None,
        use_rep: Optional[str] = None,
        method: str = 'umap',
        random_state: Optional[Union[RandomState, int]] = 0,
        write_knn_indices: bool = False,
        metric: str = 'euclidean',
        metric_kwds: Mapping[str, Any] = {}
    ) -> None:
        """\
        Compute distances and connectivities of neighbors.

        Parameters
        ----------
        n_neighbors
             Use this number of nearest neighbors.
        knn
             Restrict result to `n_neighbors` nearest neighbors.
        {n_pcs}
        {use_rep}

        Returns
        -------
        Writes sparse graph attributes `.distances` and `.connectivities`.
        Also writes `.knn_indices` and `.knn_distances` if
        `write_knn_indices==True`.
        """
        from sklearn.metrics import pairwise_distances
        start_neighbors = logg.debug('computing neighbors')
        if n_neighbors > self._adata.shape[0]:  # very small datasets
            n_neighbors = 1 + int(0.5*self._adata.shape[0])
            logg.warning(f'n_obs too small: adjusting to `n_neighbors = {n_neighbors}`')
        if method == 'umap' and not knn:
            raise ValueError('`method = \'umap\' only with `knn = True`.')
        if method not in {'umap', 'gauss'}:
            raise ValueError('`method` needs to be \'umap\' or \'gauss\'.')
        if self._adata.shape[0] >= 10000 and not knn:
            logg.warning('Using high n_obs without `knn=True` takes a lot of memory...')
        self.n_neighbors = n_neighbors
        self.knn = knn
        X = choose_representation(self._adata, use_rep=use_rep, n_pcs=n_pcs)
        # neighbor search
        use_dense_distances = (metric == 'euclidean' and X.shape[0] < 8192) or knn == False
        if use_dense_distances:
            _distances = pairwise_distances(X, metric=metric, **metric_kwds)
            knn_indices, knn_distances = get_indices_distances_from_dense_matrix(
                _distances, n_neighbors)
            if knn:
                self._distances = get_sparse_matrix_from_indices_distances_numpy(
                    knn_indices, knn_distances, X.shape[0], n_neighbors)
            else:
                self._distances = _distances
        else:
            # non-euclidean case and approx nearest neighbors
            if X.shape[0] < 4096:
                X = pairwise_distances(X, metric=metric, **metric_kwds)
                metric = 'precomputed'
            knn_indices, knn_distances, _ = compute_neighbors_umap(
                X, n_neighbors, random_state, metric=metric, metric_kwds=metric_kwds)
            #self._rp_forest = _make_forest_dict(forest)
        # write indices as attributes
        if write_knn_indices:
            self.knn_indices = knn_indices
            self.knn_distances = knn_distances
        start_connect = logg.debug('computed neighbors', time=start_neighbors)
        if not use_dense_distances or method == 'umap':
            # we need self._distances also for method == 'gauss' if we didn't
            # use dense distances
            self._distances, self._connectivities = compute_connectivities_umap(
                knn_indices,
                knn_distances,
                self._adata.shape[0],
                self.n_neighbors,
            )
        # overwrite the umap connectivities if method is 'gauss'
        # self._distances is unaffected by this
        if method == 'gauss':
            self._compute_connectivities_diffmap()
        logg.debug('computed connectivities', time=start_connect)
        self._number_connected_components = 1
        if issparse(self._connectivities):
            from scipy.sparse.csgraph import connected_components
            self._connected_components = connected_components(self._connectivities)
            self._number_connected_components = self._connected_components[0]

    def _compute_connectivities_diffmap(self, density_normalize=True):
        # init distances
        if self.knn:
            Dsq = self._distances.power(2)
            indices, distances_sq = get_indices_distances_from_sparse_matrix(
                Dsq, self.n_neighbors)
        else:
            Dsq = np.power(self._distances, 2)
            indices, distances_sq = get_indices_distances_from_dense_matrix(
                Dsq, self.n_neighbors)

        # exclude the first point, the 0th neighbor
        indices = indices[:, 1:]
        distances_sq = distances_sq[:, 1:]

        # choose sigma, the heuristic here doesn't seem to make much of a difference,
        # but is used to reproduce the figures of Haghverdi et al. (2016)
        if self.knn:
            # as the distances are not sorted
            # we have decay within the n_neighbors first neighbors
            sigmas_sq = np.median(distances_sq, axis=1)
        else:
            # the last item is already in its sorted position through argpartition
            # we have decay beyond the n_neighbors neighbors
            sigmas_sq = distances_sq[:, -1]/4
        sigmas = np.sqrt(sigmas_sq)

        # compute the symmetric weight matrix
        if not issparse(self._distances):
            Num = 2 * np.multiply.outer(sigmas, sigmas)
            Den = np.add.outer(sigmas_sq, sigmas_sq)
            W = np.sqrt(Num/Den) * np.exp(-Dsq/Den)
            # make the weight matrix sparse
            if not self.knn:
                mask = W > 1e-14
                W[mask == False] = 0
            else:
                # restrict number of neighbors to ~k
                # build a symmetric mask
                mask = np.zeros(Dsq.shape, dtype=bool)
                for i, row in enumerate(indices):
                    mask[i, row] = True
                    for j in row:
                        if i not in set(indices[j]):
                            W[j, i] = W[i, j]
                            mask[j, i] = True
                # set all entries that are not nearest neighbors to zero
                W[mask == False] = 0
        else:
            W = Dsq.copy()  # need to copy the distance matrix here; what follows is inplace
            for i in range(len(Dsq.indptr[:-1])):
                row = Dsq.indices[Dsq.indptr[i]:Dsq.indptr[i+1]]
                num = 2 * sigmas[i] * sigmas[row]
                den = sigmas_sq[i] + sigmas_sq[row]
                W.data[Dsq.indptr[i]:Dsq.indptr[i+1]] = np.sqrt(num/den) * np.exp(
                    -Dsq.data[Dsq.indptr[i]: Dsq.indptr[i+1]] / den)
            W = W.tolil()
            for i, row in enumerate(indices):
                for j in row:
                    if i not in set(indices[j]):
                        W[j, i] = W[i, j]
            W = W.tocsr()

        self._connectivities = W

    def compute_transitions(self, density_normalize=True):
        """Compute transition matrix.

        Parameters
        ----------
        density_normalize : `bool`
            The density rescaling of Coifman and Lafon (2006): Then only the
            geometry of the data matters, not the sampled density.

        Returns
        -------
        Makes attributes `.transitions_sym` and `.transitions` available.
        """
        start = logg.info('computing transitions')
        W = self._connectivities
        # density normalization as of Coifman et al. (2005)
        # ensures that kernel matrix is independent of sampling density
        if density_normalize:
            # q[i] is an estimate for the sampling density at point i
            # it's also the degree of the underlying graph
            q = np.asarray(W.sum(axis=0))
            if not issparse(W):
                Q = np.diag(1.0/q)
            else:
                Q = scipy.sparse.spdiags(1.0/q, 0, W.shape[0], W.shape[0])
            K = Q @ W @ Q
        else:
            K = W

        # z[i] is the square root of the row sum of K
        z = np.sqrt(np.asarray(K.sum(axis=0)))
        if not issparse(K):
            self.Z = np.diag(1.0/z)
        else:
            self.Z = scipy.sparse.spdiags(1.0/z, 0, K.shape[0], K.shape[0])
        self._transitions_sym = self.Z @ K @ self.Z
        logg.info('    finished', time=start)

    def compute_eigen(self, n_comps=15, sym=None, sort='decrease'):
        """Compute eigen decomposition of transition matrix.

        Parameters
        ----------
        n_comps : `int`
            Number of eigenvalues/vectors to be computed, set `n_comps = 0` if
            you need all eigenvectors.
        sym : `bool`
            Instead of computing the eigendecomposition of the assymetric
            transition matrix, computed the eigendecomposition of the symmetric
            Ktilde matrix.
        matrix : sparse matrix, np.ndarray, optional (default: `.connectivities`)
            Matrix to diagonalize. Merely for testing and comparison purposes.

        Returns
        -------
        Writes the following attributes.

        eigen_values : numpy.ndarray
            Eigenvalues of transition matrix.
        eigen_basis : numpy.ndarray
             Matrix of eigenvectors (stored in columns).  `.eigen_basis` is
             projection of data matrix on right eigenvectors, that is, the
             projection on the diffusion components.  these are simply the
             components of the right eigenvectors and can directly be used for
             plotting.
        """
        np.set_printoptions(precision=10)
        if self._transitions_sym is None:
            raise ValueError('Run `.compute_transitions` first.')
        matrix = self._transitions_sym
        # compute the spectrum
        if n_comps == 0:
            evals, evecs = scipy.linalg.eigh(matrix)
        else:
            n_comps = min(matrix.shape[0]-1, n_comps)
            # ncv = max(2 * n_comps + 1, int(np.sqrt(matrix.shape[0])))
            ncv = None
            which = 'LM' if sort == 'decrease' else 'SM'
            # it pays off to increase the stability with a bit more precision
            matrix = matrix.astype(np.float64)
            evals, evecs = scipy.sparse.linalg.eigsh(matrix, k=n_comps,
                                                  which=which, ncv=ncv)
            evals, evecs = evals.astype(np.float32), evecs.astype(np.float32)
        if sort == 'decrease':
            evals = evals[::-1]
            evecs = evecs[:, ::-1]
        logg.info('    eigenvalues of transition matrix\n'
                  '    {}'.format(str(evals).replace('\n', '\n    ')))
        if self._number_connected_components > len(evals)/2:
            logg.warning('Transition matrix has many disconnected components!')
        self._eigen_values = evals
        self._eigen_basis = evecs

    def _init_iroot(self):
        self.iroot = None
        # set iroot directly
        if 'iroot' in self._adata.uns:
            if self._adata.uns['iroot'] >= self._adata.n_obs:
                logg.warning(
                    f'Root cell index {self._adata.uns["iroot"]} does not '
                    f'exist for {self._adata.n_obs} samples. It’s ignored.'
                )
            else:
                self.iroot = self._adata.uns['iroot']
            return
        # set iroot via xroot
        xroot = None
        if 'xroot' in self._adata.uns: xroot = self._adata.uns['xroot']
        elif 'xroot' in self._adata.var: xroot = self._adata.var['xroot']
        # see whether we can set self.iroot using the full data matrix
        if xroot is not None and xroot.size == self._adata.shape[1]:
            self._set_iroot_via_xroot(xroot)

    def _get_dpt_row(self, i):
        mask = None
        if self._number_connected_components > 1:
            label = self._connected_components[1][i]
            mask = self._connected_components[1] == label
        row = sum(
            (
                self.eigen_values[l] / (1-self.eigen_values[l])
                * (self.eigen_basis[i, l] - self.eigen_basis[:, l])
            )**2
            # account for float32 precision
            for l in range(0, self.eigen_values.size)
            if self.eigen_values[l] < 0.9994
        )
        # thanks to Marius Lange for pointing Alex to this:
        # we will likely remove the contributions from the stationary state below when making
        # backwards compat breaking changes, they originate from an early implementation in 2015
        # they never seem to have deteriorated results, but also other distance measures (see e.g.
        # PAGA paper) don't have it, which makes sense
        row += sum(
            (self.eigen_basis[i, l] - self.eigen_basis[:, l])**2
            for l in range(0, self.eigen_values.size)
            if self.eigen_values[l] >= 0.9994
        )
        if mask is not None:
            row[~mask] = np.inf
        return np.sqrt(row)

    def _set_pseudotime(self):
        """Return pseudotime with respect to root point.
        """
        self.pseudotime = self.distances_dpt[self.iroot].copy()
        self.pseudotime /= np.max(self.pseudotime[self.pseudotime < np.inf])

    def _set_iroot_via_xroot(self, xroot):
        """Determine the index of the root cell.

        Given an expression vector, find the observation index that is closest
        to this vector.

        Parameters
        ----------
        xroot : np.ndarray
            Vector that marks the root cell, the vector storing the initial
            condition, only relevant for computing pseudotime.
        """
        if self._adata.shape[1] != xroot.size:
            raise ValueError(
                'The root vector you provided does not have the '
                'correct dimension.')
        # this is the squared distance
        dsqroot = 1e10
        iroot = 0
        for i in range(self._adata.shape[0]):
            diff = self._adata.X[i, :] - xroot
            dsq = diff @ diff
            if dsq < dsqroot:
                dsqroot = dsq
                iroot = i
                if np.sqrt(dsqroot) < 1e-10: break
        logg.debug(f'setting root index to {iroot}')
        if self.iroot is not None and iroot != self.iroot:
            logg.warning(f'Changing index of iroot from {self.iroot} to {iroot}.')
        self.iroot = iroot
