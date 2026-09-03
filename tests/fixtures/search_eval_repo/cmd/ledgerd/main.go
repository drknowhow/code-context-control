package main

import (
	"log"
	"net/http"

	"ledgerlite/internal/ratelimit"
)

func main() {
	limiter := ratelimit.NewLimiter(100, 20)
	if err := serveHTTP(":8081", limiter); err != nil {
		log.Fatal(err)
	}
}

func serveHTTP(addr string, limiter *ratelimit.Limiter) error {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		if !limiter.Allow() {
			w.WriteHeader(http.StatusTooManyRequests)
			return
		}
		w.WriteHeader(http.StatusOK)
	})
	return http.ListenAndServe(addr, mux)
}
