"""Password manager window: view, add, change and delete stored passwords."""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

import config
import credstore

TITLE = "VNC Unlocker - Passwords"


class PasswordWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(TITLE)
        root.minsize(620, 420)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        outer = ttk.Frame(root, padding=10)
        outer.grid(sticky="nsew")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        ttk.Label(outer, text="Credentials referenced by config.yaml (select one to set or change its password):").grid(
            row=0, column=0, sticky="w")

        list_frame = ttk.Frame(outer)
        list_frame.grid(row=1, column=0, sticky="nsew", pady=(4, 8))
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(list_frame, columns=("cred", "status", "used_by"), show="headings",
                                 selectmode="browse", height=8)
        for col, text, width, stretch in (("cred", "Credential", 220, False), ("status", "Status", 80, False),
                                          ("used_by", "Used by", 300, True)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, stretch=stretch)
        self.tree.tag_configure("missing", foreground="#c00000")
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        form = ttk.LabelFrame(outer, text="Set password", padding=8)
        form.grid(row=2, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        self.cred_var = tk.StringVar()
        self.pw1_var = tk.StringVar()
        self.pw2_var = tk.StringVar()
        self.show_var = tk.BooleanVar(value=False)
        ttk.Label(form, text="Credential:").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.cred_var).grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Label(form, text="Password:").grid(row=1, column=0, sticky="w")
        self.pw1 = ttk.Entry(form, textvariable=self.pw1_var, show="*")
        self.pw1.grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Label(form, text="Confirm:").grid(row=2, column=0, sticky="w")
        self.pw2 = ttk.Entry(form, textvariable=self.pw2_var, show="*")
        self.pw2.grid(row=2, column=1, sticky="ew", pady=2)
        ttk.Checkbutton(form, text="Show password", variable=self.show_var, command=self._toggle_show).grid(
            row=3, column=1, sticky="w")
        ttk.Label(form, foreground="#666666",
                  text="The credential name must equal an account's 'cred' in config.yaml ('user' if no 'cred' is set).").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        btns = ttk.Frame(outer)
        btns.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(btns, text="Reload config", command=self.refresh).pack(side="left")
        ttk.Button(btns, text="Close", command=root.destroy).pack(side="right")
        ttk.Button(btns, text="Delete", command=self.delete).pack(side="right", padx=4)
        ttk.Button(btns, text="Save", command=self.save).pack(side="right")
        self.status = ttk.Label(outer, text="", foreground="#006000")
        self.status.grid(row=4, column=0, sticky="w", pady=(6, 0))

        root.bind("<Return>", lambda _e: self.save())
        root.bind("<Escape>", lambda _e: root.destroy())
        self.refresh()
        self.pw1.focus_set()

    def _toggle_show(self) -> None:
        ch = "" if self.show_var.get() else "*"
        self.pw1.configure(show=ch)
        self.pw2.configure(show=ch)

    def _clear_passwords(self) -> None:
        self.pw1_var.set("")
        self.pw2_var.set("")

    def _on_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if sel and sel[0] != self.cred_var.get().strip():
            self.cred_var.set(sel[0])
            self._clear_passwords()
            self.pw1.focus_set()

    def refresh(self) -> None:
        try:
            creds = config.load().all_creds()
        except Exception as e:
            messagebox.showerror(TITLE, f"Invalid config.yaml:\n{e}", parent=self.root)
            creds = []
        selected = self.cred_var.get().strip()
        self.tree.delete(*self.tree.get_children())
        first_missing = None
        for cred, used_by in creds:
            stored = credstore.has_password(cred)
            self.tree.insert("", "end", iid=cred, values=(cred, "Stored" if stored else "Missing", used_by),
                             tags=() if stored else ("missing",))
            if not stored and first_missing is None:
                first_missing = cred
        # Never replace a name the user typed; only pick a default when the field is empty.
        target = selected or first_missing or (creds[0][0] if creds else "")
        if target and self.tree.exists(target):
            self.tree.selection_set(target)
            self.tree.see(target)
        else:
            self.tree.selection_set(())
        self.cred_var.set(target)
        missing = sum(1 for c, _ in creds if not credstore.has_password(c))
        self._set_status(f"{len(creds)} credential(s), {missing} missing" if creds else "No accounts in config.yaml",
                         ok=missing == 0)

    def _set_status(self, text: str, ok: bool = True) -> None:
        self.status.configure(text=text, foreground="#006000" if ok else "#c00000")

    def save(self) -> None:
        cred = self.cred_var.get().strip()
        if not cred:
            messagebox.showerror(TITLE, "Credential name is required.", parent=self.root)
            return
        pw = self.pw1_var.get()
        if not pw:
            messagebox.showerror(TITLE, "Password is empty.", parent=self.root)
            return
        if pw != self.pw2_var.get():
            messagebox.showerror(TITLE, "Passwords do not match.", parent=self.root)
            return
        if not self.tree.exists(cred) and not messagebox.askyesno(
                TITLE, f"'{cred}' is not used by any account in config.yaml.\nSave it anyway?", parent=self.root):
            return
        existed = credstore.has_password(cred)
        credstore.set_password(cred, pw)
        self._clear_passwords()
        self.refresh()
        self._set_status(f"{'Updated' if existed else 'Saved'} password for '{cred}'. "
                         "If a host was paused after failures, use tray menu 'Reset hosts'.")

    def delete(self) -> None:
        cred = self.cred_var.get().strip()
        if not cred:
            return
        if not credstore.has_password(cred):
            messagebox.showinfo(TITLE, f"No password stored for '{cred}'.", parent=self.root)
            return
        if messagebox.askyesno(TITLE, f"Delete the stored password for '{cred}'?", parent=self.root):
            credstore.delete_password(cred)
            self._clear_passwords()
            self.refresh()
            self._set_status(f"Deleted password for '{cred}'.")


def run() -> None:
    root = tk.Tk()
    PasswordWindow(root)
    root.mainloop()
