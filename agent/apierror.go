package main

import (
	"encoding/json"
	"net/http"
)

// ---------------------------------------------------------------------------
// One error shape, GET and HEAD only (v1.0)
// ---------------------------------------------------------------------------
//
// Every answer that is not 2xx is application/json:
//
//	{"error": "<snake_case code>", "message": "<one sentence for a person>"}
//
// The codes are part of the API (api_version 1): unauthorized,
// too_many_attempts, not_found, method_not_allowed, pools_unavailable. The
// message is for people and may be reworded. Older agents answered some
// errors with other bodies; clients go by the status code first.
//
// The API is read-only: GET and HEAD. Any other method is answered 405 with
// "Allow: GET, HEAD". HEAD answers as GET would, without the body (net/http
// drops the body of a HEAD answer).

type apiError struct {
	Error   string `json:"error"`
	Message string `json:"message"`
}

// writeError writes the one error shape with this status.
func writeError(w http.ResponseWriter, status int, code, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(apiError{Error: code, Message: message})
}

// allowGetHead refuses every method but GET and HEAD, before anything else
// (the password check included).
func allowGetHead(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			w.Header().Set("Allow", "GET, HEAD")
			writeError(w, http.StatusMethodNotAllowed, "method_not_allowed",
				"The agent's API is read-only: use GET or HEAD.")
			return
		}
		next.ServeHTTP(w, r)
	})
}

// jsonNotFound answers a path no endpoint serves with the error shape
// instead of net/http's plain-text 404. It sits inside the password check,
// so an address without the password cannot list which endpoints exist.
func jsonNotFound(mux *http.ServeMux) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, pattern := mux.Handler(r); pattern == "" {
			writeError(w, http.StatusNotFound, "not_found", "No such endpoint on this agent.")
			return
		}
		mux.ServeHTTP(w, r)
	})
}

// apiHandlerChain is the agent's whole handler: method check, then the
// password (when one is set), then the endpoints.
func apiHandlerChain(token string, mux *http.ServeMux) http.Handler {
	var handler http.Handler = jsonNotFound(mux)
	if token != "" {
		handler = authMiddleware(token, handler)
	}
	return allowGetHead(handler)
}
