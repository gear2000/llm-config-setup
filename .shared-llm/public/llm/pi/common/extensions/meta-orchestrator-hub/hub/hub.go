// hub.go — the meta-orchestrator message hub.
//
// This is the sketch's mailroom (registry + per-agent mailbox + waiter table)
// plus the three things that make it real and multi-brain:
//
//   1. OS-assigned port  — bind :0, the OS hands out a free port, so many hubs
//      can run side by side (one per brain) without colliding.
//   2. discovery JSON     — the hub writes {host, port, url, pid, started_at} to
//      a path you choose (--json). A session reads it to find its hub. The PATH
//      is the brain's identity: different brains → different JSON files.
//   3. JSON-path-is-the-lock — before starting, we take a file lock on the JSON
//      path and check whether a healthy hub is already recorded there. If so we
//      exit ("already running") and the caller just connects. Same bind-is-the-
//      lock idea, moved from a fixed port to the JSON file: one owner per JSON.
//
// Standard library only — the single static binary.
//
// Usage:
//   hub                      # default json ~/.meta-orch/hub.json, free port
//   hub --json /path/a.json  # a separate brain's hub
//
// Endpoints (unchanged from the sketch, plus /health):
//   GET  /health             POST /register     GET /next?session=W
//   POST /send               GET /await?msg=ID  POST /respond

package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"sync"
	"syscall"
	"time"
)

// ── discovery file ──────────────────────────────────────────────────────────

type Discovery struct {
	Host      string `json:"host"`
	Port      int    `json:"port"`
	URL       string `json:"url"`
	PID       int    `json:"pid"`
	StartedAt string `json:"started_at"`
}

func defaultJSONPath() string {
	home, err := os.UserHomeDir()
	if err != nil {
		home = "."
	}
	return filepath.Join(home, ".meta-orch", "hub.json")
}

func readDiscovery(path string) (Discovery, bool) {
	b, err := os.ReadFile(path)
	if err != nil {
		return Discovery{}, false
	}
	var d Discovery
	if err := json.Unmarshal(b, &d); err != nil || d.URL == "" {
		return Discovery{}, false
	}
	return d, true
}

func writeDiscovery(path string, d Discovery) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	b, err := json.MarshalIndent(d, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path) // atomic
}

func healthy(url string) bool {
	c := http.Client{Timeout: 1500 * time.Millisecond}
	resp, err := c.Get(url + "/health")
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	return resp.StatusCode == http.StatusOK
}

// claimOrConnect takes a file lock on the JSON path, then EITHER reports an
// already-running healthy hub (alreadyURL set, listener nil) OR binds a fresh
// free port, writes the discovery JSON, and returns the listener to serve on.
func claimOrConnect(jsonPath string) (alreadyURL string, ln net.Listener, err error) {
	if err = os.MkdirAll(filepath.Dir(jsonPath), 0o755); err != nil {
		return "", nil, err
	}
	lf, err := os.OpenFile(jsonPath+".lock", os.O_CREATE|os.O_RDWR, 0o644)
	if err != nil {
		return "", nil, err
	}
	defer lf.Close()
	if err = syscall.Flock(int(lf.Fd()), syscall.LOCK_EX); err != nil {
		return "", nil, err
	}
	defer syscall.Flock(int(lf.Fd()), syscall.LOCK_UN)

	// Is a healthy hub already recorded at this path? Then don't start a second.
	if d, ok := readDiscovery(jsonPath); ok && healthy(d.URL) {
		return d.URL, nil, nil
	}

	// Bind a free port (OS picks it) and record it.
	ln, err = net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return "", nil, err
	}
	port := ln.Addr().(*net.TCPAddr).Port
	d := Discovery{
		Host:      "127.0.0.1",
		Port:      port,
		URL:       "http://127.0.0.1:" + strconv.Itoa(port),
		PID:       os.Getpid(),
		StartedAt: time.Now().UTC().Format(time.RFC3339),
	}
	if err = writeDiscovery(jsonPath, d); err != nil {
		_ = ln.Close()
		return "", nil, err
	}
	return "", ln, nil
}

// ── the data the hub moves around ───────────────────────────────────────────

type Message struct {
	MsgID  string `json:"msg_id"`
	From   string `json:"from"`
	To     string `json:"to"`
	Prompt string `json:"prompt"`
}

type Reply struct {
	MsgID string `json:"msg_id"`
	Body  string `json:"body"`
	Error string `json:"error,omitempty"`
}

type Agent struct {
	SessionID string
	Outbound  chan Message // hub pushes here; the agent's /next receives
}

// ── the hub: three maps + one lock ──────────────────────────────────────────

type Hub struct {
	mu      sync.Mutex
	agents  map[string]*Agent     // registry: who's connected
	waiters map[string]chan Reply // msg_id -> sender blocked on the reply
	seq     int
}

func NewHub() *Hub {
	return &Hub{agents: map[string]*Agent{}, waiters: map[string]chan Reply{}}
}

func (h *Hub) nextID(prefix string) string {
	h.seq++
	return prefix + "-" + strconv.Itoa(h.seq)
}

func (h *Hub) health(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, map[string]any{"ok": true, "pid": os.Getpid()})
}

func (h *Hub) register(w http.ResponseWriter, r *http.Request) {
	var in struct {
		Session string `json:"session"`
	}
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil || in.Session == "" {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	h.mu.Lock()
	h.agents[in.Session] = &Agent{SessionID: in.Session, Outbound: make(chan Message, 8)}
	h.mu.Unlock()
	log.Printf("register  %s", in.Session)
	writeJSON(w, map[string]string{"ok": "true", "session": in.Session})
}

// GET /next?session=W — BLOCKS until the hub pushes an order to W.
func (h *Hub) next(w http.ResponseWriter, r *http.Request) {
	session := r.URL.Query().Get("session")
	h.mu.Lock()
	agent := h.agents[session]
	h.mu.Unlock()
	if agent == nil {
		http.Error(w, "unknown session", http.StatusNotFound)
		return
	}
	select {
	case msg := <-agent.Outbound:
		writeJSON(w, msg)
	case <-r.Context().Done():
		return
	}
}

func (h *Hub) send(w http.ResponseWriter, r *http.Request) {
	var in struct{ From, To, Prompt string }
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	h.mu.Lock()
	target := h.agents[in.To]
	if target == nil {
		h.mu.Unlock()
		http.Error(w, "unknown target", http.StatusNotFound)
		return
	}
	msg := Message{MsgID: h.nextID("msg"), From: in.From, To: in.To, Prompt: in.Prompt}
	h.waiters[msg.MsgID] = make(chan Reply, 1)
	h.mu.Unlock()

	target.Outbound <- msg
	log.Printf("send      %s -> %s  (%s)", in.From, in.To, msg.MsgID)
	writeJSON(w, map[string]string{"msg_id": msg.MsgID})
}

// GET /await?msg=ID — BLOCKS until the target submits its reply.
func (h *Hub) await(w http.ResponseWriter, r *http.Request) {
	id := r.URL.Query().Get("msg")
	h.mu.Lock()
	ch := h.waiters[id]
	h.mu.Unlock()
	if ch == nil {
		http.Error(w, "unknown msg", http.StatusNotFound)
		return
	}
	select {
	case reply := <-ch:
		h.mu.Lock()
		delete(h.waiters, id)
		h.mu.Unlock()
		writeJSON(w, reply)
	case <-r.Context().Done():
		return
	}
}

func (h *Hub) respond(w http.ResponseWriter, r *http.Request) {
	var in struct{ Msg, Body, Error string }
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	h.mu.Lock()
	ch := h.waiters[in.Msg]
	h.mu.Unlock()
	if ch == nil {
		http.Error(w, "unknown msg", http.StatusNotFound)
		return
	}
	ch <- Reply{MsgID: in.Msg, Body: in.Body, Error: in.Error}
	log.Printf("respond   %s", in.Msg)
	writeJSON(w, map[string]string{"ok": "true"})
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func main() {
	jsonPath := flag.String("json", defaultJSONPath(), "discovery JSON path (the brain's identity)")
	flag.Parse()

	already, ln, err := claimOrConnect(*jsonPath)
	if err != nil {
		log.Fatalf("hub: %v", err)
	}
	if already != "" {
		// A healthy hub is already there — the caller should just connect.
		fmt.Printf("hub already running at %s (json %s)\n", already, *jsonPath)
		return
	}

	h := NewHub()
	mux := http.NewServeMux()
	mux.HandleFunc("/health", h.health)
	mux.HandleFunc("/register", h.register)
	mux.HandleFunc("/next", h.next)
	mux.HandleFunc("/send", h.send)
	mux.HandleFunc("/await", h.await)
	mux.HandleFunc("/respond", h.respond)

	d, _ := readDiscovery(*jsonPath)
	log.Printf("hub listening on %s  (json %s, pid %d)", d.URL, *jsonPath, os.Getpid())

	// Remove the discovery JSON on a clean exit so nothing connects to a dead hub.
	cleanup := func() { _ = os.Remove(*jsonPath) }
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sig
		cleanup()
		os.Exit(0)
	}()

	srv := &http.Server{Handler: mux}
	if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
		cleanup()
		log.Fatalf("hub: %v", err)
	}
}
