######################
# Model:
# binsize: float, bin size
# binunit: str, unit of binsize

# Params:
# N: int, number of observation channels (neuron, LFP, ...)
# M: int, number of factors
# P: int, number of regressors
# C: Array(N, M + P), loading matrix [Cf, Cr]
# scale: Array(M,), factor scales
# lengthscale: Array(M,), factor lengthscales

# Trial:
# tid: [int, str], unique identifier
# y: Array(T, N), observations
# x: Array(T, R), regressors
# mu: Array(T, M), factors
# v: Array(T, M), diagonals of V matrices (posterior covariance)
# w: Array(T, N), diagonals of W matrices
# K: Array(M, T, T), prior kernel matrices, reference to some irredundant storage
# L: Array(M, T, S), K ~= LL'
######################
import math
import time
from collections import Iterable
from typing import Union, Sequence, Tuple, Any, Callable

import click
from jax import numpy as jnp
from sklearn.decomposition import FactorAnalysis

from .data import Session, Params, Trial
from .util import diag_embed, stable_solve as solve, capped_exp

__all__ = ['Inference']


def trial_update(y, Cx, Cz, x, z, v, w, K, logdet, eps) -> Tuple[float, Any]:
    T = y.shape[0]
    u = v @ (Cz ** 2)
    lnr = x @ Cx + z @ Cz
    r = capped_exp(lnr + 0.5 * u)  # [x, z] C
    # assert not jnp.any(jnp.isnan(r))
    w = r @ (Cz.T ** 2)
    # assert not jnp.any(jnp.isnan(w))
    z3d = jnp.expand_dims(z.T, -1)
    K_div_z = solve(K, z3d)
    # assert not jnp.any(jnp.isnan(K_div_z))

    invw = 1 / (w + eps)
    invW = diag_embed(invw.T)  # (zdim, T, T)
    V = K - K @ solve(invW + K, K)  # (zdim, T, T)
    # assert not jnp.any(jnp.isnan(V))
    ll = jnp.sum(r - y * lnr)  # likelihood
    lp = 0.5 * jnp.sum(logdet +
                       jnp.squeeze(jnp.transpose(z3d, (0, 2, 1)) @ K_div_z, -1) +
                       jnp.trace(jnp.linalg.solve(K, V), axis1=-2, axis2=-1))
    lq = -0.5 * jnp.sum(jnp.log(jnp.linalg.cholesky(V).diagonal(axis1=-2, axis2=-1)).sum(-1) * 2)
    # l2 = jnp.sum((jnp.sum(z ** 2, 0) - T) ** 2)
    l2 = 0.
    loss = (ll + lp + lq + l2) / T

    # Newton step
    g = K_div_z - jnp.expand_dims(Cz @ (y - r).T, -1)  # (zdim, T, T) (zdim, T, 1)
    # z3d2 = jnp.sum(z3d ** 2, axis=1, keepdims=True)
    # assert not jnp.any(jnp.isnan(z3d2))
    # g += (2 * z3d2 - T) * z3d
    # Hl2 = 6 * z3d2 - T
    # assert not jnp.any(jnp.isnan(g))
    # invH = V - V @ solve(1 / (Hl2 + eps) + V, V)
    step = jnp.squeeze(V @ g, -1).T  # V = inv(-Hessian)
    # assert not jnp.any(jnp.isnan(step))
    return loss, step


def estep(session, params, *, max_iter: int = 20, stepsize=.5, eps: float = 1e-8) -> jnp.ndarray:
    zdim = params.n_factors
    C = params.C  # (zdim + xdim, ydim)
    Cz, Cx = jnp.vsplit(C, [zdim])  # (n_factors + n_regressors, n_channels)

    total_loss: jnp.ndarray = jnp.array(0.)
    total_T = 0
    for trial in session:  # parallelizable
        x = trial.x  # regressors
        z = trial.z
        y = trial.y
        v = trial.v
        w = trial.w
        K = trial.K
        logdet = trial.logdet

        T = y.shape[0]
        loss = jnp.inf
        for i in range(max_iter):
            new_loss, newton_step = trial_update(y, Cx, Cz, x, z, v, w, K, logdet, eps)

            if jnp.isclose(loss, new_loss):
                break
            loss = new_loss

            if jnp.any(jnp.isnan(newton_step)) or jnp.any(jnp.isinf(newton_step)):
                break
            z -= stepsize * newton_step

        trial.z = z
        trial.w = w
        total_T += T
        total_loss += loss * T

    return total_loss / total_T


def mstep(session, params, *, max_iter: int = 20, stepsize=.5):
    zdim = params.n_factors
    C = params.C  # (zdim + xdim, ydim)

    # concat trials
    y = session.y
    x = session.x
    z = session.z
    v = session.v
    M = jnp.column_stack((z, x))

    loss = jnp.inf
    for i in range(max_iter):
        Cz = C[:zdim, :]
        u = v @ (Cz ** 2)
        lnr = M @ C
        r = capped_exp(lnr + 0.5 * u)
        assert not jnp.any(jnp.isnan(r))
        l1 = jnp.mean(jnp.sum(r - y * lnr, axis=-1))
        new_loss = l1

        if jnp.isclose(loss, new_loss):
            break
        loss = new_loss

        R = diag_embed(r.T)  # (ydim, T, T)
        # Newton update
        g = (r - y).T @ M  # (ydim, zdim + xdim)
        H = jnp.expand_dims(M.T, 0) @ R @ jnp.expand_dims(M, 0)  # (ydim, zdim + xdim, zdim + xdim)
        step = jnp.squeeze(solve(H, jnp.expand_dims(g, -1)), -1).T  # (ydim, ?, 1)
        C -= stepsize * step

    params.C = C
    return loss


def preprocess(session, params, initialize):
    for trial in session:
        T = trial.y.shape[0]
        trial.z = jnp.asarray(initialize(trial.y))
        assert trial.z.shape[0] == T
        trial.v = jnp.tile(params.scale, (T, 1))
        trial.w = jnp.ones_like(trial.z)
        trial.K = params.K[T]
        trial.L = params.L[T]
        trial.logdet = params.logdet[T]


def make_em_session(session, T) -> Session:
    y = session.y
    x = session.x
    em_session = Session(session.binsize, session.unit)
    n_trials = y.shape[0] // T
    y = y[:n_trials * T]
    x = x[:n_trials * T]
    for i, (yi, xi) in enumerate(zip(jnp.split(y, n_trials), jnp.split(x, n_trials))):
        em_session.add_trial(Trial(i, y=yi, x=xi))

    return em_session


class Inference:
    def __init__(self, session: Session,
                 n_factors: int,
                 kernel: Union[Callable, Sequence[Callable]], *,
                 scale: Union[float, Sequence[float]] = 1.,
                 lengthscale: Union[float, Sequence[float]] = 1.):
        self.session = session
        self.params = Params(n_factors, scale, lengthscale)
        self.kernel = kernel
        self.em_session = None  # for quick EM
        self.init()

    def init(self):
        assert self.session.trials
        trial = self.session.trials[0]
        n_channels = trial.y.shape[-1]
        n_regressors = trial.x.shape[-1]
        n_factors = self.params.n_factors

        fa = FactorAnalysis(n_components=self.params.n_factors)
        y = self.session.y
        fa = fa.fit(y)

        T_em = math.floor(max(self.params.lengthscale) / self.session.binsize)
        self.em_session = make_em_session(self.session, T_em)

        # init params
        self.params.C = jnp.zeros((n_factors + n_regressors, n_channels))
        Ts = [trial.y.shape[0] for trial in self.session]
        Ts.append(T_em)
        unique_Ts = jnp.unique(Ts)
        ks = self.kernel if isinstance(self.kernel, Iterable) else [self.kernel] * n_factors
        self.params.K = {
            T: jnp.stack([k(jnp.arange(T * self.session.binsize, step=self.session.binsize)) for k in ks]) for T
            in unique_Ts}
        self.params.L = {
            T: jnp.linalg.cholesky(K)
            for T, K in self.params.K.items()
        }
        self.params.logdet = {
            T: jnp.log(L.diagonal(axis1=-2, axis2=-1)).sum(-1) * 2
            for T, L in self.params.L.items()
        }

        # init trials
        click.echo('Initializing')
        preprocess(self.session, self.params, initialize=fa.transform)
        preprocess(self.em_session, self.params, initialize=fa.transform)

    def fit(self, *, max_iter: int = 20, tol: float = 1e-7):
        click.echo('EM starting')
        loss = jnp.inf
        for i in range(max_iter):
            tick = time.perf_counter()
            m_loss = mstep(self.em_session, self.params)
            tock = time.perf_counter()
            m_elapsed = tock - tick

            tick = time.perf_counter()
            e_loss = estep(self.em_session, self.params)
            tock = time.perf_counter()
            e_elapsed = tock - tick

            click.echo(
                f'EM Iteration {i + 1},\tLoss = {e_loss.item():.2f},\t'
                f'M step: {m_elapsed:.2f}s,\t'
                f'E step: {e_elapsed:.2f}s'
            )

            if jnp.isnan(e_loss):
                click.echo('EM stopped at NaN loss.')
                break
            if jnp.isclose(loss, e_loss):
                click.echo('EM stopped at unchanged loss.')
                break
            if e_loss > loss:
                click.echo('EM stopped at increased loss.')
                break

            loss = e_loss

        click.echo('Inferring')
        estep(self.session, self.params)
        click.echo('Finished')

        return self
