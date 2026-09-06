package server

import (
	"context"
	"fmt"
	"net/http"
)

const (
	DefaultPort = 8080
	MaxConns    = 64
)

// Server wraps an http.Server with an address.
type Server struct {
	Addr string
	srv  *http.Server
}

// New builds a Server listening on addr.
func New(addr string) *Server {
	return &Server{Addr: addr}
}

// Start runs the server until ctx is done.
func (s *Server) Start(ctx context.Context) error {
	s.srv = &http.Server{Addr: s.Addr}
	go func() {
		<-ctx.Done()
		s.srv.Close()
	}()
	return s.srv.ListenAndServe()
}

func (s *Server) String() string {
	return fmt.Sprintf("Server(%s)", s.Addr)
}

func healthHandler(w http.ResponseWriter, r *http.Request) {
	fmt.Fprintln(w, "ok")
}
