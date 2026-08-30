"""Minimal Isolation Forest (pure numpy) for detector D6 — behavioural novelty.

Deliberately dependency-light (no scikit-learn) so the whole backend installs
cleanly on any Python and in a slim container. Returns an anomaly score in
[0,1]; ~1 means "isolates quickly" = novel/anomalous.
"""
from __future__ import annotations

import math
import numpy as np


def _c(n: int) -> float:
    if n <= 1:
        return 1.0
    H = math.log(n - 1) + 0.5772156649
    return 2 * H - (2 * (n - 1) / n)


class _Node:
    __slots__ = ("feat", "split", "left", "right", "size", "depth")

    def __init__(self):
        self.feat = -1
        self.split = 0.0
        self.left = None
        self.right = None
        self.size = 0
        self.depth = 0


class IsolationForest:
    def __init__(self, n_trees: int = 100, sample: int = 256, seed: int = 7):
        self.n_trees = n_trees
        self.sample = sample
        self.rng = np.random.default_rng(seed)
        self.trees: list[_Node] = []
        self.psi = 0

    def _build(self, X: np.ndarray, depth: int, max_depth: int) -> _Node:
        node = _Node()
        node.size = len(X)
        node.depth = depth
        if depth >= max_depth or len(X) <= 1:
            return node
        # random feature with non-zero spread
        feats = self.rng.permutation(X.shape[1])
        for f in feats:
            lo, hi = X[:, f].min(), X[:, f].max()
            if hi > lo:
                node.feat = int(f)
                node.split = float(self.rng.uniform(lo, hi))
                mask = X[:, f] < node.split
                node.left = self._build(X[mask], depth + 1, max_depth)
                node.right = self._build(X[~mask], depth + 1, max_depth)
                return node
        return node  # all identical -> external

    def fit(self, X: np.ndarray) -> "IsolationForest":
        X = np.asarray(X, dtype=float)
        if X.ndim == 1:
            X = X.reshape(-1, 1)
        n = len(X)
        self.psi = min(self.sample, n)
        max_depth = max(1, int(math.ceil(math.log2(max(2, self.psi)))))
        self.trees = []
        for _ in range(self.n_trees):
            idx = self.rng.choice(n, self.psi, replace=(self.psi > n))
            self.trees.append(self._build(X[idx], 0, max_depth))
        return self

    def _path(self, x: np.ndarray, node: _Node) -> float:
        if node.left is None or node.size <= 1:
            return node.depth + _c(node.size)
        if x[node.feat] < node.split:
            return self._path(x, node.left)
        return self._path(x, node.right)

    def score(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype=float)
        if X.ndim == 1:
            X = X.reshape(-1, 1)
        cpsi = _c(self.psi)
        out = np.empty(len(X))
        for i, x in enumerate(X):
            avg = np.mean([self._path(x, t) for t in self.trees])
            out[i] = 2 ** (-avg / (cpsi or 1.0))
        return out
