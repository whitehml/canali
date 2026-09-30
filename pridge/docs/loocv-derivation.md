# Leave-one-out identity

To choose a good shrinkage strength ($\lambda$), pRidge asks how well a fit predicts a match it never saw. It asks
with leave-one-out cross-validation: hold out one alliance's score, refit on the rest, and predict the held-out score.
Done literally, an event with 80 alliance rows needs 80 refits per candidate $\lambda$.

There is a shortcut when the prior being regressed toward is fixed across refits. `Fit.loocv_residuals` returns every
leave-one-out error from the one full fit, with no refits, and the result is identical to the refits. This document
derives that shortcut and states when it holds.

## Setup

Each row of the data is one alliance in one qualification match. $X$ has a column per team and a 1 where the team played
on that alliance. $y$ is the alliance's score. $\beta$ is the vector of team ratings the fit produces, and $\beta_0$ is
the prior, one starting guess per team, taken from pre-event EPA. $\lambda$ says how hard the fit is pulled toward
$\beta_0$: near zero, the event's matches decide everything; large, the prior does.

Write $x_i$ for row $i$ of $X$ as a column vector. Then:

```math
\begin{aligned}
A &= X^\top X + \lambda I \\
s &= X^\top y + \lambda \beta_0 \\
\beta &= A^{-1} s
\end{aligned}
```

That is the whole fit. $X^\top X$ is the match evidence. $\lambda I$ and $\lambda \beta_0$ are the prior, weighted as
if it were $\lambda$ extra observations per team. $A$ can always be inverted when $\lambda > 0$, even at the start of
an event when there are fewer matches than teams and OPR alone could not be computed.

Two by-products of the fit matter below:

```math
\begin{aligned}
e &= y - X\beta && \text{how far off the fit is on each row it was trained on} \\
h_i &= x_i^\top A^{-1} x_i && \text{leverage: how much row } i \text{ pulled the fit toward itself}
\end{aligned}
```

Leverage is a number between 0 and 1. A row with high leverage bends the fit toward its own score, so the fit looks
better on that row than it would on a fresh one. The training error $e_i$ is therefore too optimistic, and leverage
measures by how much.

## Deleting a row

Fit again with row $i$ removed. Only the match evidence loses that row. The prior terms
$\lambda I$ and $\lambda \beta_0$ do not involve any row, so they stay:

```math
\begin{aligned}
A_{-i} &= A - x_i x_i^\top \\
s_{-i} &= s - x_i y_i
\end{aligned}
```

Removing one row changes $A$ by a rank-one piece, and the Sherman-Morrison formula inverts such a change without
starting over:

```math
A_{-i}^{-1} = A^{-1} + \frac{A^{-1} x_i x_i^\top A^{-1}}{1 - h_i}
```

Multiplying out $\beta_{-i} = A_{-i}^{-1} s_{-i}$ and substituting $\beta = A^{-1} s$:

```math
\begin{aligned}
\beta_{-i} &= \beta - A^{-1} x_i y_i \\
&\quad + \frac{A^{-1} x_i \, x_i^\top \beta}{1 - h_i} - \frac{A^{-1} x_i \, h_i y_i}{1 - h_i} \\
&= \beta + A^{-1} x_i \, \frac{x_i^\top \beta - y_i}{1 - h_i} \\
&= \beta - A^{-1} x_i \, \frac{e_i}{1 - h_i}
\end{aligned}
```

In words: the fit without row $i$ differs from the full fit by a nudge, and the nudge is built from the training error
on that row, $e_i$, scaled up by $1 / (1 - h_i)$. Every symbol on the right is already known from the full fit.

## The identity

Use the fit that never saw row $i$ to predict row $i$:

```math
x_i^\top \beta_{-i} = x_i^\top \beta - \frac{h_i e_i}{1 - h_i}
```

The leave-one-out residual is the actual score minus that prediction:

```math
y_i - x_i^\top \beta_{-i} = e_i + \frac{h_i e_i}{1 - h_i} = \frac{e_i}{1 - h_i}
```

So the honest error on a row is its training error divided by $1 - h_i$. A row the fit barely bent toward ($h_i$ near 0)
keeps its training error. A row the fit bent hard toward ($h_i$ near 1) has its error inflated, because most of its
apparent fit was the row explaining itself. `Fit.loocv_mse` is the mean of these squared errors, and `fit_grid` picks
the $\lambda$ that minimizes it.

## Conditions

**$1 - h_i > 0$.** The division above must be safe. With $\lambda > 0$, the fit without row $i$ still has the prior in
it: $A_{-i} = X_{-i}^\top X_{-i} + \lambda I$ is positive definite, and that forces $h_i < 1$. Leverage reaching 1 can
only be numerical, and `SingularFitError` reports it. Ordinary least squares has no such guarantee: a row that alone
identifies a team has leverage 1, and there is no fit left to predict it with.

**$\beta_0$ is fixed across folds.** The derivation deletes a row from the match evidence and leaves $\lambda \beta_0$
alone, which is the same as saying the prior would be identical in every refit. That holds when $\beta_0$ does not
depend on the rows being left out. Pre-event EPA is computed before the event starts, so no match of the event feeds it.
A prior that absorbed the event's own matches would make the shortcut approximate, because each refit would then have a
slightly different prior.

## Computation

`fit` needs $A^{-1}$ at every candidate $\lambda$. `Gram` computes the eigendecomposition
$X^\top X = Q\,\mathrm{diag}(d)\,Q^\top$ once, after which

```math
A^{-1} = Q \, \mathrm{diag}\!\left(\frac{1}{d + \lambda}\right) Q^\top
```

for any $\lambda$ costs one matrix product. `fit_grid` builds it once and sweeps $\lambda$, scoring each candidate with
`loocv_mse`.

The cache is per response: `Gram` holds $X^\top y$, so a fit to a different response, such as one scoring component
instead of the total, needs its own. The prior enters only at `fit`.
