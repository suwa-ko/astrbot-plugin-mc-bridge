package com.astrbot.bridge;

import net.minecraft.client.Minecraft;

/** Throttle rendering only; normal client ticks, networking and mod logic keep running. */
final class BackgroundRun {
    private static long renderUntil;
    private BackgroundRun() {}
    static void requestFrame() {
        Minecraft mc=Minecraft.getInstance();var window=mc.getWindow();
        // GLFW can report a 1x1 framebuffer when the client starts minimized.
        // Allocate an offscreen-sized render target; no OS window restore/focus is needed.
        if(window.getWidth()<640||window.getHeight()<360) {
            window.setWidth(1280);window.setHeight(720);mc.resizeDisplay();
        }
        renderUntil=System.currentTimeMillis()+2000;tick();
    }
    static void tick() {
        Minecraft mc=Minecraft.getInstance();
        boolean inactive=!mc.isWindowActive()
            ||org.lwjgl.glfw.GLFW.glfwGetWindowAttrib(mc.getWindow().getWindow(),org.lwjgl.glfw.GLFW.GLFW_ICONIFIED)==org.lwjgl.glfw.GLFW.GLFW_TRUE
            ||org.lwjgl.glfw.GLFW.glfwGetWindowAttrib(mc.getWindow().getWindow(),org.lwjgl.glfw.GLFW.GLFW_FOCUSED)==org.lwjgl.glfw.GLFW.GLFW_FALSE;
        boolean background=mc.level!=null&&BridgeConfig.BACKGROUND_MODE.get()&&inactive;
        // Loading and respawn screens may finish their lifecycle from render().
        // Keep UI frames at the background FPS; suppress only the idle in-world view.
        boolean rendering=!background||mc.screen!=null||System.currentTimeMillis()<renderUntil;
        mc.noRender=!rendering;
        mc.getWindow().setFramerateLimit(background?BridgeConfig.BACKGROUND_FPS.get():mc.options.framerateLimit().get());
    }
    static boolean renderingSuppressed(){return Minecraft.getInstance().noRender;}
}
