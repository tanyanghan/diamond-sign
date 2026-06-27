/*
 * mcnotifier events behavior pack.
 *
 * Emits player chat and death events to the server console as marker lines:
 *   MCNOTIFIER {"t":"death",...}
 *   MCNOTIFIER {"t":"chat",...}
 *
 * console.warn() goes to the dedicated server's stdout, which (with
 * content-log-console-output-enabled=true in server.properties) is captured to
 * console.log and tailed by the bot. No HTTP / @minecraft/server-net needed.
 *
 * Deaths use afterEvents.entityDie (stable — no experiment required).
 * Chat uses beforeEvents.chatSend (experimental — needs the "Beta APIs"
 * experiment enabled on the world); it's guarded so deaths still work without it.
 */
import { world } from "@minecraft/server";

function emit(obj) {
  console.warn("MCNOTIFIER " + JSON.stringify(obj));
}

// --- Deaths (stable) ---
world.afterEvents.entityDie.subscribe((e) => {
  const d = e.deadEntity;
  if (!d || d.typeId !== "minecraft:player") return;
  const src = e.damageSource || {};
  emit({
    t: "death",
    player: d.name,
    cause: src.cause || "unknown",
    by: src.damagingEntity ? src.damagingEntity.typeId : undefined,
  });
});

// --- Chat (experimental: requires the Beta APIs experiment) ---
// Guarded so the pack still loads (and deaths still work) when the experiment
// is off, in which case world.beforeEvents.chatSend is unavailable.
try {
  world.beforeEvents.chatSend.subscribe((e) => {
    // Read-only use: do NOT set e.cancel — we only observe.
    emit({ t: "chat", player: e.sender ? e.sender.name : "?", msg: e.message });
  });
} catch (err) {
  console.warn("MCNOTIFIER chatSend unavailable (enable Beta APIs for chat): " + err);
}
