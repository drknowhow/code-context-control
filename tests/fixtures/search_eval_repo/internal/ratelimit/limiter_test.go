package ratelimit

import "testing"

func TestAllowRespectsBurst(t *testing.T) {
	l := NewLimiter(1, 2)
	if !l.Allow() || !l.Allow() {
		t.Fatal("burst of 2 should allow two requests")
	}
	if l.Allow() {
		t.Fatal("third request should be denied")
	}
}
