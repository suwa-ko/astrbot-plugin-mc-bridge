package com.astrbot.bridge;

import com.mojang.logging.LogUtils;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.bus.api.IEventBus;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.ModContainer;
import net.neoforged.fml.common.Mod;
import net.neoforged.fml.config.ModConfig;
import net.neoforged.neoforge.client.event.ClientChatReceivedEvent;
import net.neoforged.neoforge.client.event.ClientTickEvent;
import net.neoforged.neoforge.common.NeoForge;
import org.slf4j.Logger;

/** Client-side bridge entry point. */
@Mod(value = AstrBotBridge.MOD_ID, dist = Dist.CLIENT)
public final class AstrBotBridge {
    public static final String MOD_ID = "astrbot_bridge";
    public static final Logger LOGGER = LogUtils.getLogger();

    public AstrBotBridge(IEventBus modEventBus, ModContainer modContainer) {
        modContainer.registerConfig(ModConfig.Type.CLIENT, BridgeConfig.SPEC);
        NeoForge.EVENT_BUS.register(this);
        LOGGER.info("[astrbot-bridge] client bridge loaded");
    }

    @SubscribeEvent(priority = net.neoforged.bus.api.EventPriority.LOWEST)
    public void beforeClientTick(ClientTickEvent.Pre event) {
        BridgeClient.get().beforeTick();
    }

    @SubscribeEvent
    public void onClientTick(ClientTickEvent.Post event) {
        BridgeClient.get().tick();
    }

    @SubscribeEvent
    public void afterRender(net.neoforged.neoforge.client.event.RenderFrameEvent.Post event) {
        BridgeClient.get().afterFrame();
    }

    @SubscribeEvent
    public void onChat(ClientChatReceivedEvent event) {
        BridgeClient.get().onGameChat(event);
    }
}
