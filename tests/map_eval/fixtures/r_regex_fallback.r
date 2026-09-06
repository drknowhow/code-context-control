library(stats)
library(utils)

DEFAULT_ALPHA <- 0.05

# Compute the mean of a numeric vector, ignoring NA.
safe_mean <- function(x, na.rm = TRUE) {
  if (length(x) == 0) {
    return(NA_real_)
  }
  mean(x, na.rm = na.rm)
}

# Standardize a vector.
zscore <- function(x) {
  (x - safe_mean(x)) / sd(x, na.rm = TRUE)
}

# Run a t-test and report whether it is significant.
p_value <- function(a, b, alpha = DEFAULT_ALPHA) {
  result <- t.test(a, b)
  result$p.value < alpha
}

main <- function() {
  sample <- rnorm(10)
  print(zscore(sample))
}
