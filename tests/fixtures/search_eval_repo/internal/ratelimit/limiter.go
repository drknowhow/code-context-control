package ratelimit

import (
	"sync"
	"time"
)

// Limiter is a token bucket: rate tokens per second, burst capacity.
type Limiter struct {
	mu     sync.Mutex
	rate   float64
	burst  float64
	tokens float64
	last   time.Time
}

// NewLimiter returns a full bucket.
func NewLimiter(rate, burst float64) *Limiter {
	return &Limiter{rate: rate, burst: burst, tokens: burst, last: time.Now()}
}

// Allow reports whether one request may proceed now.
func (l *Limiter) Allow() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := time.Now()
	l.tokens += now.Sub(l.last).Seconds() * l.rate
	if l.tokens > l.burst {
		l.tokens = l.burst
	}
	l.last = now
	if l.tokens < 1 {
		return false
	}
	l.tokens--
	return true
}
