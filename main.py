"""
main.py - User interface for the P2P network assignment.

    python main.py                      -> graphical interface (Tkinter)
    python main.py --cli                -> simple command-line interface
    python main.py --cli --name Alice --port 5000

If Tkinter is not installed, the command-line interface is used automatically.
"""

import argparse
import os
import queue
import threading

from p2p_node import P2PNode

try:
    import tkinter as tk
    from tkinter import filedialog, ttk
    HAS_TK = True
except ImportError:          # some Linux installs ship Python without Tk
    HAS_TK = False

DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")


# =============================================================================
# Graphical interface
# =============================================================================
class PeerApp:
    def __init__(self, root):
        self.root = root
        self.node = None
        self.peer_ids = []                  # same order as the listbox rows
        self.events = queue.Queue()         # node threads -> GUI thread

        root.title("UAP P2P Network")
        root.geometry("900x620")
        root.minsize(720, 520)
        self._build_ui()
        self._set_running(False)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._process_events)

    # ------------------------------------------------------------------- UI
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # My Peer
        frame = ttk.LabelFrame(self.root, text="My Peer")
        frame.pack(fill="x", **pad)
        row = ttk.Frame(frame)
        row.pack(fill="x", padx=6, pady=4)
        ttk.Label(row, text="Name:").pack(side="left")
        self.name_var = tk.StringVar(value="Alice")
        self.name_entry = ttk.Entry(row, textvariable=self.name_var, width=16)
        self.name_entry.pack(side="left", padx=(4, 16))
        ttk.Label(row, text="Port:").pack(side="left")
        self.port_var = tk.StringVar(value="5000")
        self.port_entry = ttk.Entry(row, textvariable=self.port_var, width=8)
        self.port_entry.pack(side="left", padx=(4, 16))
        self.start_btn = ttk.Button(row, text="Start Peer", command=self.start_peer)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(row, text="Stop", command=self.stop_peer)
        self.stop_btn.pack(side="left", padx=4)
        self.info_var = tk.StringVar(value="Peer not started")
        ttk.Label(frame, textvariable=self.info_var).pack(anchor="w", padx=6, pady=(0, 4))

        # Connect to another peer
        frame = ttk.LabelFrame(self.root, text="Connect to Another Peer")
        frame.pack(fill="x", **pad)
        row = ttk.Frame(frame)
        row.pack(fill="x", padx=6, pady=4)
        ttk.Label(row, text="IP:").pack(side="left")
        self.ip_var = tk.StringVar(value="127.0.0.1")
        self.ip_entry = ttk.Entry(row, textvariable=self.ip_var, width=18)
        self.ip_entry.pack(side="left", padx=(4, 16))
        ttk.Label(row, text="Port:").pack(side="left")
        self.rport_var = tk.StringVar(value="5001")
        self.rport_entry = ttk.Entry(row, textvariable=self.rport_var, width=8)
        self.rport_entry.pack(side="left", padx=(4, 16))
        self.connect_btn = ttk.Button(row, text="Connect", command=self.connect_peer)
        self.connect_btn.pack(side="left", padx=4)

        # The bottom panels are packed BEFORE the expanding middle section so they
        # always keep their space when the window is small.
        # Status bar
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(self.root, textvariable=self.status_var, relief="sunken",
                  anchor="w").pack(fill="x", side="bottom")

        # Send file
        frame = ttk.LabelFrame(self.root, text="Send File")
        frame.pack(fill="x", side="bottom", **pad)
        row = ttk.Frame(frame)
        row.pack(fill="x", padx=6, pady=4)
        ttk.Label(row, text="Text, image, audio, video, PDF, ZIP, etc.").pack(side="left")
        self.file_btn = ttk.Button(row, text="Choose File & Send", command=self.send_file)
        self.file_btn.pack(side="right")

        # Send text
        frame = ttk.LabelFrame(self.root, text="Send Text")
        frame.pack(fill="x", side="bottom", **pad)
        row = ttk.Frame(frame)
        row.pack(fill="x", padx=6, pady=4)
        self.msg_var = tk.StringVar()
        self.msg_entry = ttk.Entry(row, textvariable=self.msg_var)
        self.msg_entry.pack(side="left", fill="x", expand=True)
        self.msg_entry.bind("<Return>", lambda _e: self.send_text())
        self.send_btn = ttk.Button(row, text="Send", command=self.send_text)
        self.send_btn.pack(side="left", padx=(8, 0))

        # Peer list + event log
        middle = ttk.Frame(self.root)
        middle.pack(fill="both", expand=True, **pad)

        left = ttk.LabelFrame(middle, text="Connected Peers")
        left.pack(side="left", fill="y")
        self.peer_list = tk.Listbox(left, width=34, height=8, exportselection=False)
        self.peer_list.pack(fill="both", expand=True, padx=4, pady=4)

        right = ttk.LabelFrame(middle, text="Messages / Events")
        right.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.log_box = tk.Text(right, state="disabled", wrap="word", height=8,
                               font=("Consolas", 10))
        scroll = ttk.Scrollbar(right, command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_box.pack(fill="both", expand=True, padx=(4, 0), pady=4)
        self.log_box.tag_configure("error", foreground="#b00020")
        self.log_box.tag_configure("system", foreground="#1a5fb4")
        self.log_box.tag_configure("file", foreground="#26734d")


    def _set_running(self, running):
        """Enable/disable widgets depending on whether the peer is running."""
        self.start_btn.config(state="disabled" if running else "normal")
        self.stop_btn.config(state="normal" if running else "disabled")
        for w in (self.name_entry, self.port_entry):
            w.config(state="disabled" if running else "normal")
        state = "normal" if running else "disabled"
        for w in (self.ip_entry, self.rport_entry, self.connect_btn,
                  self.msg_entry, self.send_btn, self.file_btn):
            w.config(state=state)

    # ----------------------------------------------------- thread-safe events
    def _node_log(self, text):          # called from network threads
        self.events.put(("log", text))

    def _node_peers_changed(self):      # called from network threads
        self.events.put(("peers", None))

    def _process_events(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self._append_log(value)
                elif kind == "peers":
                    self._refresh_peers()
                elif kind == "status":
                    self.status_var.set(value)
        except queue.Empty:
            pass
        self.root.after(100, self._process_events)

    def _append_log(self, text):
        tag = None
        if text.startswith("[ERROR]"):
            tag = "error"
        elif text.startswith("[SYSTEM]"):
            tag = "system"
        elif "File received" in text or "File sent" in text:
            tag = "file"
        self.log_box.config(state="normal")
        self.log_box.insert("end", text + "\n", tag)
        self.log_box.see("end")
        self.log_box.config(state="disabled")
        if "File received" in text:
            self.status_var.set(f"Received: downloads{os.sep}{text.rsplit(': ', 1)[-1]}")
        elif text.startswith("[ERROR]"):
            self.status_var.set(text)

    def _refresh_peers(self):
        selected = self._selected_peer_id()
        self.peer_list.delete(0, "end")
        self.peer_ids = []
        if not self.node:
            return
        for peer in self.node.list_peers():
            self.peer_list.insert("end", f"{peer.label} {peer.ip}:{peer.port}")
            self.peer_ids.append(peer.peer_id)
        if selected in self.peer_ids:
            self.peer_list.selection_set(self.peer_ids.index(selected))

    def _selected_peer_id(self):
        selection = self.peer_list.curselection()
        if not selection or selection[0] >= len(self.peer_ids):
            return None
        return self.peer_ids[selection[0]]

    # --------------------------------------------------------------- actions
    def start_peer(self):
        name = self.name_var.get().strip()
        if not name:
            self._append_log("[ERROR] Please enter a peer name")
            return
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            self._append_log("[ERROR] Invalid port: please enter a number (e.g. 5000)")
            return
        node = P2PNode(name, port, self._node_log, self._node_peers_changed, DOWNLOAD_DIR)
        try:
            node.start()
        except (ValueError, OSError) as exc:
            self._append_log(f"[ERROR] {exc}")
            return
        self.node = node
        self.info_var.set(f"{node.name} | ID: {node.peer_id} | Port: {node.port}")
        self.status_var.set(f"Peer started: {node.name} [{node.peer_id}] on port {node.port}")
        self._set_running(True)

    def stop_peer(self):
        if self.node:
            self.node.stop()
            self.node = None
        self._refresh_peers()
        self.info_var.set("Peer not started")
        self.status_var.set("Peer stopped")
        self._set_running(False)

    def _run_in_thread(self, func):
        """Network calls can block, so they run off the GUI thread."""
        def worker():
            try:
                func()
            except Exception as exc:    # shown in the log, never crashes the app
                self.events.put(("log", f"[ERROR] {exc}"))
        threading.Thread(target=worker, daemon=True).start()

    def connect_peer(self):
        if not self.node:
            return
        ip, port = self.ip_var.get(), self.rport_var.get()
        node = self.node

        def work():
            try:
                node.connect(ip, port)
            except (ValueError, ConnectionError) as exc:
                self.events.put(("log", f"[ERROR] Connection failed: {exc}"))
        self.status_var.set(f"Connecting to {ip}:{port} ...")
        self._run_in_thread(work)

    def send_text(self):
        if not self.node:
            return
        peer_id = self._selected_peer_id()
        if peer_id is None:
            self._append_log("[ERROR] Select a connected peer first")
            return
        message = self.msg_var.get().strip()
        if not message:
            return
        self.msg_var.set("")
        node = self.node
        self._run_in_thread(lambda: node.send_text(peer_id, message))

    def send_file(self):
        if not self.node:
            return
        peer_id = self._selected_peer_id()
        if peer_id is None:
            self._append_log("[ERROR] Select a connected peer first")
            return
        path = filedialog.askopenfilename(title="Choose a file to send")
        if not path:
            return
        node = self.node
        name = os.path.basename(path)

        def progress(sent, total):
            percent = 100 if total == 0 else int(sent * 100 / total)
            self.events.put(("status", f"Sending {name}: {percent}%"))

        def work():
            node.send_file(peer_id, path, progress)
            self.events.put(("status", f"Sent {name}"))
        self.status_var.set(f"Sending {name} ...")
        self._run_in_thread(work)

    def on_close(self):
        if self.node:
            self.node.stop()
        self.root.destroy()


def run_gui():
    root = tk.Tk()
    PeerApp(root)
    root.mainloop()


# =============================================================================
# Command-line interface (fallback when Tkinter is not available)
# =============================================================================
CLI_HELP = """Commands:
  peers                    list connected peers
  connect <ip> <port>      connect to another peer
  msg <n> <text>           send text to peer number <n> from the list
  send <n> <file path>     send a file to peer number <n>
  help                     show this help
  quit                     stop the peer and exit"""


def run_cli(name=None, port=None):
    name = name or input("Peer name: ").strip()
    while True:
        try:
            port = int(port if port is not None else input("Listening port: ").strip())
            break
        except ValueError:
            print("[ERROR] Invalid port")
            port = None

    node = P2PNode(name, port, log_callback=print, download_dir=DOWNLOAD_DIR)
    try:
        node.start()
    except (ValueError, OSError) as exc:
        print(f"[ERROR] {exc}")
        return
    print(CLI_HELP)

    def pick(token):
        peers = node.list_peers()
        try:
            return peers[int(token) - 1]
        except (ValueError, IndexError):
            print("[ERROR] Select a connected peer first (see 'peers')")
            return None

    try:
        while True:
            try:
                line = input().strip()
            except EOFError:
                break
            if not line:
                continue
            command, _, rest = line.partition(" ")
            command = command.lower()
            try:
                if command in ("quit", "exit"):
                    break
                elif command == "help":
                    print(CLI_HELP)
                elif command == "peers":
                    peers = node.list_peers()
                    if not peers:
                        print("No connected peers")
                    for i, peer in enumerate(peers, 1):
                        print(f"  {i}. {peer}")
                elif command == "connect":
                    parts = rest.split()
                    if len(parts) != 2:
                        print("Usage: connect <ip> <port>")
                    else:
                        node.connect(parts[0], parts[1])
                elif command == "msg":
                    token, _, text = rest.partition(" ")
                    peer = pick(token)
                    if peer and text.strip():
                        node.send_text(peer.peer_id, text.strip())
                elif command == "send":
                    token, _, path = rest.partition(" ")
                    peer = pick(token)
                    if peer:
                        node.send_file(peer.peer_id, path.strip().strip('"'))
                else:
                    print("Unknown command. Type 'help'.")
            except (ValueError, ConnectionError, FileNotFoundError) as exc:
                print(f"[ERROR] {exc}")
    except KeyboardInterrupt:
        pass
    finally:
        node.stop()


def main():
    parser = argparse.ArgumentParser(description="UAP P2P Network")
    parser.add_argument("--cli", action="store_true", help="use the command-line interface")
    parser.add_argument("--name", help="peer name (CLI mode)")
    parser.add_argument("--port", type=int, help="listening port (CLI mode)")
    args = parser.parse_args()

    if args.cli or not HAS_TK:
        if not args.cli:
            print("Tkinter is not available - using the command-line interface.")
        run_cli(args.name, args.port)
    else:
        run_gui()


if __name__ == "__main__":
    main()
