#!/usr/bin/env python3
from pathlib import Path
import sys

p = Path(sys.argv[1]) / "app/Madeira/ContentView.swift"
s = p.read_text(encoding="utf-8")

appear = '''            .onAppear {
                jit_install_trap_handler()'''
replacement = '''            .onAppear {
                recoverFEXCrashTrace()
                jit_install_trap_handler()'''
if appear not in s:
    raise SystemExit("Could not add FEX crash trace recovery to onAppear")
s = s.replace(appear, replacement, 1)

anchor = '''    private func logEntitlementStatus() {'''
helper = r'''    private func recoverFEXCrashTrace() {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        let url = docs.appendingPathComponent("madeira-crash-checkpoints.txt")
        guard let trace = try? String(contentsOf: url, encoding: .utf8), !trace.isEmpty else {
            logStore.log("No previous FEX crash trace found", level: .debug)
            return
        }
        logStore.log("=== Recovered FEX crash trace ===", level: .error)
        for line in trace.split(separator: "\n").suffix(80) {
            logStore.log(String(line), level: .error)
        }
        logStore.log("=== End recovered trace ===", level: .error)
    }

'''
if anchor not in s:
    raise SystemExit("Could not insert FEX crash trace recovery helper")
p.write_text(s.replace(anchor, helper + anchor, 1), encoding="utf-8")
print("Installed relaunch crash-trace recovery UI")
