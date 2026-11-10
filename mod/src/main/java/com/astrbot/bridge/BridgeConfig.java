package com.astrbot.bridge;

import net.neoforged.neoforge.common.ModConfigSpec;

/** Client config for the AstrBot bridge. */
public final class BridgeConfig {
    public static final ModConfigSpec SPEC;
    public static final ModConfigSpec.ConfigValue<String> URL;
    public static final ModConfigSpec.ConfigValue<String> TOKEN;
    public static final ModConfigSpec.BooleanValue AUTO_CONNECT;
    public static final ModConfigSpec.BooleanValue FORWARD_CHAT;
    public static final ModConfigSpec.BooleanValue BACKGROUND_MODE;
    public static final ModConfigSpec.IntValue BACKGROUND_FPS;
    public static final ModConfigSpec.BooleanValue AUTO_RESPAWN;
    public static final ModConfigSpec.ConfigValue<String> NON_HOSTILE_ENTITIES;

    static {
        ModConfigSpec.Builder builder = new ModConfigSpec.Builder();
        builder.push("bridge");
        URL = builder
                .comment("AstrBot WebSocket URL, e.g. ws://127.0.0.1:8766/ws or wss://your.domain/mc-bridge/ws")
                .define("url", "ws://127.0.0.1:8766/ws");
        TOKEN = builder
                .comment("Bearer token. Must match astrbot_plugin_mc_bridge token.")
                .define("token", "change-me");
        AUTO_CONNECT = builder
                .comment("Automatically connect when a world/server is joined.")
                .define("autoConnect", true);
        FORWARD_CHAT = builder
                .comment("Forward incoming game chat to AstrBot.")
                .define("forwardChat", true);
        BACKGROUND_MODE=builder.comment("Skip world rendering while unfocused; screenshots briefly restore rendering. Client ticks and networking are never paused.").define("backgroundMode",true);
        BACKGROUND_FPS=builder.comment("Background client frames per second; simulation still advances at normal 20 ticks per second.").defineInRange("backgroundFps",10,5,30);
        AUTO_RESPAWN=builder.comment("Normal automatic respawn after death. Cancels old goals; does not restore items, teleport or resume work.").define("autoRespawn",true);
        NON_HOSTILE_ENTITIES=builder.comment("Comma-separated entity IDs that mods may register as MONSTER but are passive; excluded from hostiles, auto combat and survival events. Example: genshin_nature:luminousseelie").define("nonHostileEntities","genshin_nature:luminousseelie");
        builder.pop();
        SPEC = builder.build();
    }

    private BridgeConfig() {
    }
}
