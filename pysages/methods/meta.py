# SPDX-License-Identifier: MIT
# Copyright (c) 2020-2021: PySAGES contributors
# See LICENSE.md and CONTRIBUTORS.md at https://github.com/SSAGESLabs/PySAGES

from typing import Callable, Mapping, NamedTuple, Optional

from pysages.collective_variables import Angle, DihedralAngle, get_periods, wrap
from pysages.methods.core import SamplingMethod, generalize
from pysages.utils import JaxArray, gaussian
from pysages.grids import build_indexer

from jax import numpy as np, lax, grad, value_and_grad, vmap

# ================ #
#   Metadynamics   #
# ================ #


class MetadynamicsState(NamedTuple):
    """
    Description of bias by metadynamics bias potential for a CV.

    Attributes
    ----------

    bias:
        Array of metadynamics bias forces for each particle in the simulation.

    xi:
        Collective variable value in the last simulation step.

    centers:
        Centers of the accumulated gaussians.

    sigmas:
        Widths of the accumulated gaussians.

    heights:
        Height values for all accumulated gaussians (zeros for not yet added gaussians).

    bias_grad:
        Array of metadynamics bias gradients for each particle in the simulation stored on a grid.

    bias_pot:
        Array of metadynamics bias potentials stored on a grid.

    idx: int
        Index of the next gaussian to be deposited.

    nstep:
        Counts the number of times `method.update` has been called.
    """

    bias: JaxArray
    xi: JaxArray
    centers: JaxArray
    sigmas: JaxArray
    heights: JaxArray
    bias_grad: JaxArray
    bias_pot: JaxArray
    idx: int
    nstep: int

    def __repr__(self):
        return repr("PySAGES" + type(self).__name__)


class Metadynamics(SamplingMethod):
    """
    Sampling method for metadynamics.
    """

    snapshot_flags = {"positions", "indices"}

    def __init__(self, cvs, height, sigma, stride, ngaussians, deltaT=None, *args, **kwargs):
        """
        Description of method parameters.

        Arguments
        ---------

        cvs: Collective variable.

        height: Initial Gaussian height.

        sigma: Initial width of Gaussian.

        stride: Bias potential deposition stride.

        ngaussians: Total number of expected gaussians (timesteps // stride + 1).

        deltaT: Optional[float] = None
            Well-tempered metadynamics $\\Delta T$ parameter (if `None` standard metadynamics is used).

        Keyword arguments
        -----------------

        kB: Boltzmann constant.

        grid: CV on a grid.
        """

        super().__init__(cvs, args, kwargs)

        self.cvs = cvs
        self.height = height
        self.sigma = sigma
        self.stride = stride
        self.ngaussians = ngaussians  # TODO: infer from timesteps and stride
        self.deltaT = deltaT

        # TODO: remove this if we eventually extract kB from the backend
        self.kB = kwargs.get("kB", 8.314462618e-3)
        self.grid = kwargs.get("grid", None)

    def build(self, snapshot, helpers):
        self.helpers = helpers
        return metadynamics(self, snapshot, helpers)


def metadynamics(method, snapshot, helpers):
    """
    Initialization and update of bias forces. Interface as expected for methods.
    """
    cv = method.cv
    height = method.height
    sigma = method.sigma
    stride = method.stride
    ngaussians = method.ngaussians
    deltaT = method.deltaT
    kB = method.kB
    dt = snapshot.dt
    natoms = np.size(snapshot.positions, 0)

    grid_centers = None
    if method.grid is not None:
        grid = method.grid
        dims = grid.shape.size
        grid_centers = construct_grid(grid)
        get_grid_index = build_indexer(grid)

    deposit_gaussian = build_gaussian_accumulator(method)
    evaluate_bias_grad = build_bias_grad_evaluator(method)

    def initialize():
        # initialize bias forces and calculate initial CV
        bias = np.zeros((natoms, 3), dtype=np.float64)
        xi, _ = cv(helpers.query(snapshot))

        # TODO: for restart; use hills file to initialize corresponding arrays.
        # initial arrays to store CV centers, sigma and height of Gaussians at the stride
        centers = np.zeros((ngaussians, xi.shape[1]), dtype=np.float64)
        sigmas = np.array(sigma, dtype=np.float64)
        heights = np.zeros(ngaussians, dtype=np.float64)

        # initialize arrays to store forces and bias potential on a grid.
        bias_grad = (
            None
            if method.grid is None
            else np.zeros((*method.grid.shape + 1, dims), dtype=np.float64)
        )
        bias_pot = (
            None if method.grid is None else np.zeros((*method.grid.shape + 1,), dtype=np.float64)
        )

        return MetadynamicsState(bias, xi, centers, sigmas, heights, bias_grad, bias_pot, 0, 0)

    def update(state, data):
        # calculate CV and the gradient of bias potential along CV -- dBias/dxi
        xi, Jxi = cv(data)
        index_xi = None if method.grid is None else get_grid_index(xi)
        bias = state.bias

        # deposit bias potential -- store cv centers, sigma, height depending on stride
        centers, sigmas, heights, idx, bias_grad, bias_pot = lax.cond(
            ((state.nstep + 1) % stride == 0),
            deposit_gaussian,
            lambda s: (
                s[0].centers,
                s[0].sigmas,
                s[0].heights,
                s[0].idx,
                s[0].bias_grad,
                s[0].bias_pot,
            ),
            (state, xi, index_xi, height, deltaT, kB, grid_centers),
        )

        # evaluate gradient of bias
        gradA = evaluate_bias_grad(xi, index_xi, centers, sigmas, heights, bias_grad)

        # calculate bias forces
        bias = -Jxi.T @ gradA.flatten()
        bias = bias.reshape(state.bias.shape)

        return MetadynamicsState(
            bias, xi, centers, sigmas, heights, bias_grad, bias_pot, idx, state.nstep + 1
        )

    return snapshot, initialize, generalize(update, helpers, jit_compile=True)



### Helpers

def construct_grid(grid):
    # generate centers of the grid points for storing bias potential and its gradient
    indicesArray = np.indices(grid.shape + 1)
    grid_indices = np.stack((*lax.map(np.ravel, indicesArray),)).T
    grid_spacing = np.divide(grid.upper - grid.lower, grid.shape)
    coordinate = grid.lower + np.multiply(grid_indices, grid_spacing)
    return np.flip(coordinate, axis=1)


def build_gaussian_accumulator(method: Metadynamics):
    periods = get_periods(method.cvs)

    # Select method for storing cv centers or storing bias on a grid

    def update_well_tempered_height(params):
        # update cv centers, heights in non-grid approach
        state, xi, index_xi, height_initial, deltaT, kB, grid_centers = params
        centers = state.centers
        centers = state.centers.at[state.idx].set(xi[0])
        heights = state.heights
        heights = heights.at[state.idx].set(height_initial)

        # update heights if well-tempered
        if deltaT is not None:
            exp_prod = gaussian(1, state.sigmas, wrap(xi - centers, periods))
            net_bias_pot = 0.0
            deltaT_kB = deltaT * kB
            # fori_loop seems better here than recursive functions
            def bias_wmeta(_local_idx, _net_bias_pot):
                _net_bias_pot += (
                    height_initial * np.exp(-_net_bias_pot / deltaT_kB) * exp_prod[_local_idx]
                )
                return _net_bias_pot

            net_bias_pot = lax.fori_loop(0, state.idx, bias_wmeta, net_bias_pot)
            heights = heights.at[state.idx].set(
                height_initial * np.exp(-net_bias_pot / (deltaT * kB))
            )
        return centers, state.sigmas, heights, state.idx + 1, state.bias_grad, state.bias_pot

    # update cv centers, heights, bias and gradient of bias stored on the grid
    def update_grid_bias_grad(params):
        state, xi, index_xi, height_initial, deltaT, kB, grid_centers = params
        centers = state.centers
        centers = state.centers.at[state.idx].set(xi[0])
        heights = state.heights
        heights = heights.at[state.idx].set(height_initial)
        current_height = height_initial
        bias_grad = state.bias_grad
        bias_pot = state.bias_pot
        current_sigmas = state.sigmas[state.idx]

        # update heights if well-tempered
        if deltaT is not None:
            current_height = height_initial * np.exp(-bias_pot[index_xi] / (deltaT * kB))
            heights = heights.at[state.idx].set(current_height)

        # update bias potential and bias gradient on the grid
        def evaluate_potential(xi, centers):
            return sum_of_gaussians(xi, centers, current_height, current_sigmas, periods)[0]

        # evaluate bias potential and gradient of the bias using autograd
        _bias_pot, _bias_grad = vmap(value_and_grad(evaluate_potential), in_axes=(0, None))(
            grid_centers, xi
        )
        bias_pot += _bias_pot.reshape(bias_pot.shape)  # reshape converts to grid format
        bias_grad += _bias_grad.reshape(bias_grad.shape)  # reshape converts to grid format

        return centers, state.sigmas, heights, state.idx + 1, bias_grad, bias_pot

    # select method
    if method.grid is None:
        return update_well_tempered_height
    else:
        return update_grid_bias_grad


def build_bias_grad_evaluator(method: Metadynamics):
    # Select evaluation method for calculating the gradient of the bias
    if method.grid is None:
        periods = get_periods(method.cvs)

        # Non-grid-based approach
        def evaluate_bias_grad(xi, idx, centers, sigmas, heights, bias_grad):
            return grad(sum_of_gaussians)(xi, centers, heights, sigmas, periods)
    else:

        def evaluate_bias_grad(xi, idx, centers, sigmas, heights, bias_grad):
            return bias_grad[idx]

    return evaluate_bias_grad


# Helper function to evaluate bias potential -- may be moved to analysis part
def sum_of_gaussians(xi, centers, heights, sigmas, periods):
    """
    Sum of n-dimensional gaussians potential.
    """
    delta_x = wrap(xi - centers, periods)
    return gaussian(heights, sigmas, delta_x).sum()
