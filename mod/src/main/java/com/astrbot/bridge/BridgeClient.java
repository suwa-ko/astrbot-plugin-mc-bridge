package com.astrbot.bridge;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.mojang.blaze3d.platform.NativeImage;
import net.minecraft.client.Minecraft;
import net.minecraft.client.Screenshot;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.commands.arguments.EntityAnchorArgument;
import net.minecraft.core.component.DataComponents;
import net.minecraft.core.registries.BuiltInRegistries;
import net.neoforged.neoforge.client.event.ClientChatReceivedEvent;
import java.util.concurrent.atomic.AtomicLong;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.MobCategory;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.item.ItemStack;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.WebSocket;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Comparator;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.TimeUnit;

/** WebSocket client living on the Minecraft client thread. */
public final class BridgeClient {
    private static final BridgeClient INSTANCE = new BridgeClient();

    private final HttpClient http = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(10))
            .build();
    private final Object sendLock = new Object();
    private CompletableFuture<Void> sendTail = CompletableFuture.completedFuture(null);
    private final AtomicLong actionGeneration = new AtomicLong();
    private final SurvivalActions survival = new SurvivalActions(this::sendResult, () -> this.socket, actionGeneration::get);
    private String pendingCapture;
    private WebSocket captureOrigin;
    private long captureDeadline;
    private long tickSampleStarted=System.nanoTime();
    private int sampledTicks;
    private double clientTps;
    private long eatingGeneration=-1,eatingDeadline,eatingStarted;
    private WebSocket eatingOrigin;
    private int eatingFoodBefore;
    private long eatingTraceNext;
    private LocalPlayer respawnRequestedFor;
    private LocalPlayer controlledActor,observedDeath;

    private volatile WebSocket socket;
    private volatile boolean started;
    private volatile boolean autoConnect = true;
    private volatile boolean forwardChat = true;
    private volatile boolean connecting;
    private volatile long nextReconnect;
    private volatile int attempts;
    private volatile String url = "";
    private volatile String token = "";

    private BridgeClient() {
    }

    public static BridgeClient get() {
        return INSTANCE;
    }

    public void beforeTick() {
        survival.beforeTick();
        if(eatingGeneration<0)return;
        Minecraft mc=Minecraft.getInstance();
        if(mc.player==null||socket!=eatingOrigin||actionGeneration.get()!=eatingGeneration||System.currentTimeMillis()>eatingDeadline
                ||mc.player.getFoodData().getFoodLevel()>eatingFoodBefore||!mc.player.isUsingItem()) {
            AstrBotBridge.LOGGER.info("[astrbot-bridge] eat lease ended elapsed={}ms using={} food={} sameGeneration={} screen={}",
                System.currentTimeMillis()-eatingStarted,mc.player!=null&&mc.player.isUsingItem(),mc.player==null?-1:mc.player.getFoodData().getFoodLevel(),actionGeneration.get()==eatingGeneration,mc.screen==null?"none":mc.screen.getClass().getSimpleName());
            eatingGeneration=-1;mc.options.keyUse.setDown(false);return;
        }
        mc.options.keyUse.setDown(true);
    }

    public void tick() {
        sampledTicks++;
        long tickNow=System.nanoTime();
        if(tickNow-tickSampleStarted>5000000000L){clientTps=sampledTicks*1000000000.0/(tickNow-tickSampleStarted);sampledTicks=0;tickSampleStarted=tickNow;}
        BackgroundRun.tick();
        if(pendingCapture!=null&&(socket!=captureOrigin||System.currentTimeMillis()>captureDeadline)) {
            if(socket==captureOrigin)sendResult(pendingCapture,false,"fresh render capture timed out",null);
            pendingCapture=null;
        }
        if(eatingGeneration>=0 && "survival-20261101".equals(System.getProperty("MinecraftBot.acceptance","")) && System.currentTimeMillis()>=eatingTraceNext) {
            Minecraft mc=Minecraft.getInstance();eatingTraceNext=System.currentTimeMillis()+250;
            AstrBotBridge.LOGGER.info("[astrbot-bridge] eat input trace key={} using={} windowActive={} context={}",mc.options.keyUse.isDown(),mc.player!=null&&mc.player.isUsingItem(),mc.isWindowActive(),mc.options.keyUse.getKeyConflictContext());
        }
        normalAutoRespawn();
        survival.tick();
        if (!started) {
            start();
        }
        if (!autoConnect) {
            return;
        }
        Minecraft mc=Minecraft.getInstance();
        if(mc.player==null||mc.level==null||!mc.level.hasChunkAt(mc.player.blockPosition())) {
            if(socket!=null){WebSocket previous=socket;disconnect(previous);previous.sendClose(1000,"left game world");}
            return;
        }
        if (socket == null && !connecting && System.currentTimeMillis() >= nextReconnect) {
            connect();
        }
    }

    private void start() {
        started = true;
        try {
            url = BridgeConfig.URL.get();
            token = BridgeConfig.TOKEN.get();
            autoConnect = BridgeConfig.AUTO_CONNECT.get();
            forwardChat = BridgeConfig.FORWARD_CHAT.get();
        } catch (Exception exc) {
            AstrBotBridge.LOGGER.warn("[astrbot-bridge] config not ready: {}", exc.toString());
        }
        if (url == null || url.isBlank()) {
            autoConnect = false;
        }
    }

    private void connect() {
        String target = url;
        if (target == null || target.isBlank()) {
            return;
        }
        connecting = true;
        try {
            WebSocket.Builder builder = http.newWebSocketBuilder()
                    .connectTimeout(Duration.ofSeconds(10));
            if (token != null && !token.isBlank()) {
                builder.header("Authorization", "Bearer " + token);
            }
            builder.buildAsync(URI.create(target), new Listener())
                    .whenComplete((ws, err) -> {
                        connecting = false;
                        if (err != null || ws == null) {
                            AstrBotBridge.LOGGER.warn(
                                    "[astrbot-bridge] connect failed: {}",
                                    err == null ? "null websocket" : err.toString());
                            scheduleReconnect();
                            return;
                        }
                        if (socket == ws) {
                            AstrBotBridge.LOGGER.info("[astrbot-bridge] connected to {}", target);
                        }
                    });
        } catch (Exception exc) {
            connecting = false;
            AstrBotBridge.LOGGER.warn("[astrbot-bridge] connect error: {}", exc.toString());
            scheduleReconnect();
        }
    }

    private void scheduleReconnect() {
        attempts = Math.min(attempts + 1, 10);
        long delay = Math.min(30_000L, 5_000L * attempts);
        nextReconnect = System.currentTimeMillis() + delay;
    }

    private final class Listener implements WebSocket.Listener {
        private final StringBuilder buffer = new StringBuilder();

        @Override
        public void onOpen(WebSocket webSocket) {
            socket = webSocket;
            attempts = 0;
            nextReconnect = 0;
            synchronized (sendLock) { sendTail = CompletableFuture.completedFuture(null); }
            webSocket.request(1);
            Minecraft.getInstance().execute(() -> { if (socket == webSocket) sendHello(webSocket); });
        }

        @Override
        public CompletionStage<?> onText(WebSocket webSocket, CharSequence data, boolean last) {
            if (buffer.length() + data.length() > 65536) {
                disconnect(webSocket);
                webSocket.abort();
                return null;
            }
            buffer.append(data);
            if (last) {
                String message = buffer.toString();
                buffer.setLength(0);
                handle(message, webSocket);
            }
            webSocket.request(1);
            return null;
        }

        @Override
        public CompletionStage<?> onClose(WebSocket webSocket, int statusCode, String reason) {
            disconnect(webSocket);
            AstrBotBridge.LOGGER.info(
                    "[astrbot-bridge] disconnected: {} {}", statusCode, reason);
            return null;
        }

        @Override
        public void onError(WebSocket webSocket, Throwable error) {
            disconnect(webSocket);
            AstrBotBridge.LOGGER.warn("[astrbot-bridge] websocket error: {}", error.getClass().getSimpleName());
        }
    }

    private void sendHello(WebSocket ws) {
        try {
            JsonObject hello = new JsonObject();
            hello.addProperty("type", "hello");
            hello.addProperty("client", "astrbot-bridge");
            hello.addProperty("version", "0.7.1");
            hello.addProperty("local_collect_resources","logs,stone,coal,iron,wheat");
            hello.addProperty("protocol", 3);
            hello.addProperty("capabilities", "inventory,equip,unequip_armor,craft,workbench,scan,mine_block,sleep,navigate,operation,knowledge,sense,station,smelt,interact,pickup,fresh_capture,background_render,look_angles,station_recovery,manual_craft,normal_respawn,normal_auto_respawn,path_feedback,collision_navigation,move_item,trade,ride,dismount,wake,plant,till,jump,dig_down,block_context,get_menu,interact_block,release_use,mine_below,align,use_bonemeal,ladder_assist,navigation_break_budget");
            Minecraft mc = Minecraft.getInstance();
            hello.addProperty(
                    "player",
                    mc.player == null ? "" : mc.player.getGameProfile().getName());
            send(hello);
        } catch (Exception exc) {
            AstrBotBridge.LOGGER.warn("[astrbot-bridge] hello failed: {}", exc.toString());
        }
    }

    private void handle(String raw, WebSocket origin) {
        if (socket != origin) return;
        try {
            JsonObject obj = JsonParser.parseString(raw).getAsJsonObject();
            String type = obj.has("type") ? obj.get("type").getAsString() : "";
            if ("action".equals(type)) {
                Minecraft.getInstance().execute(() -> { if (socket == origin) handleAction(obj); });
            } else if ("ping".equals(type)) {
                JsonObject pong = new JsonObject();
                pong.addProperty("type", "pong");
                send(pong);
            } else if ("hello".equals(type)) {
                AstrBotBridge.LOGGER.info("[astrbot-bridge] server hello");
            }
        } catch (Exception exc) {
            AstrBotBridge.LOGGER.warn("[astrbot-bridge] bad message: {}", exc.toString());
        }
    }

    private void handleAction(JsonObject obj) {
        String id = optString(obj, "id", "");
        String action = optString(obj, "action", "");
        JsonObject params = obj.has("params") && obj.get("params").isJsonObject()
                ? obj.getAsJsonObject("params")
                : new JsonObject();
        LocalPlayer player = Minecraft.getInstance().player;
        if (player == null) {
            sendResult(id, false, "player not in world", null);
            return;
        }
        try {
            if (SurvivalActions.handles(action)) {
                survival.handle(id, action, params, player);
                return;
            }
            boolean stopping = action.equals("baritone") && optString(params, "command", "").trim().equals("stop");
            if (survival.busy() && !stopping && !java.util.Set.of("status", "screenshot", "chat", "scan_blocks", "scan_entities", "inspect_block").contains(action)) {
                sendResult(id, false, "survival action running; stop it first", null);
                return;
            }
            switch (action) {
                case "normal_respawn" -> {
                    if(!params.has("confirm")||!params.get("confirm").getAsBoolean()||!canNormalRespawn(player.isDeadOrDying(),Minecraft.getInstance().screen instanceof net.minecraft.client.gui.screens.DeathScreen))throw new IllegalStateException("explicit confirmation and the death screen are required; no respawn was requested");
                    actionGeneration.incrementAndGet();survival.cancel("explicit normal respawn");
                    respawnRequestedFor=player;player.respawn();sendResult(id,true,null,simple("accepted",true));
                }
                case "status" -> sendResult(id, true, null, status(player));
                case "chat" -> {
                    String text = optString(params, "text", "");
                    if (text.isBlank()) {
                        sendResult(id, false, "empty text", null);
                    } else if (text.length() > 240) {
                        sendResult(id, false, "text too long", null);
                    } else if (text.stripLeading().matches("^[#/／＃].*")) {
                        sendResult(id, false, "chat only; command text rejected", null);
                    } else {
                        player.connection.sendChat(text);
                        sendResult(id, true, null, simple("sent", true));
                    }
                }
                case "baritone" -> {
                    String command = optString(params, "command", "").trim();
                    if (!validBaritone(command)) {
                        sendResult(id, false, "bad baritone command", null);
                    } else if (!baritoneTargetVisible(player, command)) {
                        sendResult(id, false, "附近没有这个玩家，无法跟随", null);
                    } else {
                        actionGeneration.incrementAndGet();
                        survival.cancel("interrupted by movement/stop");
                        Minecraft.getInstance().options.keyUse.setDown(false);
                        if(command.startsWith("goto ")||command.startsWith("follow "))survival.prepareNavigation();
                        if(command.startsWith("goto ")){String[] parts=command.split(" ");survival.observeNavigation(new net.minecraft.core.BlockPos(Integer.parseInt(parts[1]),Integer.parseInt(parts[2]),Integer.parseInt(parts[3])));}
                        if(!command.equals("stop"))player.connection.sendChat("#" + command);
                        sendResult(id, true, null, simple("accepted", true));
                    }
                }
                case "look" -> {
                    if(params.has("yaw")&&params.has("pitch")) {
                        float yaw=params.get("yaw").getAsFloat(),pitch=params.get("pitch").getAsFloat();
                        if(!Float.isFinite(yaw+pitch)||Math.abs(yaw)>360||Math.abs(pitch)>90)throw new IllegalArgumentException("invalid camera angle");
                        player.setYRot(yaw);player.setYHeadRot(yaw);player.setXRot(pitch);
                        JsonObject result=new JsonObject();result.addProperty("yaw",player.getYRot());result.addProperty("pitch",player.getXRot());sendResult(id,true,null,result);
                    } else if(params.has("x")&&params.has("y")&&params.has("z")) {
                        net.minecraft.core.BlockPos target=new net.minecraft.core.BlockPos(params.get("x").getAsInt(),params.get("y").getAsInt(),params.get("z").getAsInt());
                        if(player.getEyePosition().distanceTo(net.minecraft.world.phys.Vec3.atCenterOf(target))>64)throw new IllegalArgumentException("look target is too far");
                        player.lookAt(EntityAnchorArgument.Anchor.EYES,net.minecraft.world.phys.Vec3.atCenterOf(target));
                        JsonObject result=new JsonObject();result.addProperty("yaw",player.getYRot());result.addProperty("pitch",player.getXRot());result.add("position",new JsonObject());result.getAsJsonObject("position").addProperty("x",target.getX());result.getAsJsonObject("position").addProperty("y",target.getY());result.getAsJsonObject("position").addProperty("z",target.getZ());sendResult(id,true,null,result);
                    } else look(id, player, optString(params, "player", ""));
                }
                case "come" -> come(id, player, optString(params, "player", ""));
                case "attack" -> attack(id, player, optString(params, "target", ""));
                case "eat" -> eat(id, player);
                case "screenshot" -> screenshot(id);
                case "knowledge" -> sendResult(id,true,null,GameKnowledge.query(player,params));
                case "sense" -> sendResult(id,true,null,GameSense.observe(player));
                default -> sendResult(id, false, "unknown action: " + action, null);
            }
        } catch (Exception exc) {
            AstrBotBridge.LOGGER.warn("[astrbot-bridge] action {} failed: {}", action, exc.toString());
            sendResult(id, false, exc.toString(), null);
        }
    }

    static boolean canNormalRespawn(boolean dead,boolean deathScreen){return dead&&deathScreen;}

    static boolean shouldAutoRespawn(boolean enabled,boolean dead,boolean deathScreen,boolean alreadyRequested){return enabled&&dead&&deathScreen&&!alreadyRequested;}
    private void normalAutoRespawn(){
        Minecraft mc=Minecraft.getInstance();LocalPlayer player=mc.player;
        if(player==null){respawnRequestedFor=null;controlledActor=null;observedDeath=null;return;}
        if(controlledActor!=null&&controlledActor!=player)actorTransition("actor_replaced");
        controlledActor=player;
        if(!player.isDeadOrDying()){respawnRequestedFor=null;observedDeath=null;return;}
        if(observedDeath!=player){observedDeath=player;actorTransition("player_died");}
        if(!shouldAutoRespawn(BridgeConfig.AUTO_RESPAWN.get(),true,mc.screen instanceof net.minecraft.client.gui.screens.DeathScreen,respawnRequestedFor==player))return;
        respawnRequestedFor=player;player.respawn();AstrBotBridge.LOGGER.info("[astrbot-bridge] normal automatic respawn requested; old goals cancelled");
    }
    private void actorTransition(String reason){
        Minecraft mc=Minecraft.getInstance();actionGeneration.incrementAndGet();survival.cancel(reason);
        mc.options.keyUp.setDown(false);mc.options.keyShift.setDown(false);mc.options.keyUse.setDown(false);mc.options.keyAttack.setDown(false);
        JsonObject notice=new JsonObject();notice.addProperty("type","event");notice.addProperty("event","normal_respawn");JsonObject detail=new JsonObject();detail.addProperty("reason",reason);detail.addProperty("at_ms",System.currentTimeMillis());notice.add("data",detail);send(notice);
    }

    private boolean baritoneTargetVisible(LocalPlayer player, String command) {
        String prefix = "follow player ";
        if (!command.startsWith(prefix)) {
            return true;
        }
        String name = command.substring(prefix.length()).trim();
        if (name.isBlank() || player.level() == null) {
            return false;
        }
        return player.level().players().stream()
                .anyMatch(other -> other.getGameProfile().getName().equalsIgnoreCase(name));
    }

    private JsonObject status(LocalPlayer player) {
        JsonObject data = new JsonObject();
        data.addProperty("player", player.getGameProfile().getName());
        JsonObject pos = new JsonObject();
        pos.addProperty("x", player.getX());
        pos.addProperty("y", player.getY());
        pos.addProperty("z", player.getZ());
        data.add("position", pos);
        data.addProperty("dimension", player.level().dimension().location().toString());
        String world = Minecraft.getInstance().getCurrentServer() == null
                ? "singleplayer:" + (Minecraft.getInstance().getSingleplayerServer() == null ? "unknown" : Minecraft.getInstance().getSingleplayerServer().getWorldData().getLevelName())
                : Minecraft.getInstance().getCurrentServer().ip;
        try {
            data.addProperty("world_key", java.util.HexFormat.of().formatHex(java.security.MessageDigest.getInstance("SHA-256").digest(world.getBytes(java.nio.charset.StandardCharsets.UTF_8))).substring(0, 24));
        } catch (java.security.NoSuchAlgorithmException impossible) { throw new IllegalStateException(impossible); }
        data.addProperty("sleeping", player.isSleeping());
        data.addProperty("passenger",player.isPassenger());
        data.addProperty("on_ground",player.onGround());
        data.addProperty("day_time", player.level().getDayTime());
        data.addProperty("can_sleep", player.level().dimension() == net.minecraft.world.level.Level.OVERWORLD && player.level().dimensionType().bedWorks()
                && (SurvivalRules.night(player.level().getDayTime()) || player.level().isThundering()));
        data.addProperty("survival_busy", survival.busy());
        data.add("navigation_protection",survival.navigationState());
        data.add("path_feedback",survival.pathState());
        data.addProperty("selected_slot", player.getInventory().selected);
        data.addProperty("using_item", player.isUsingItem());
        data.addProperty("client_paused", Minecraft.getInstance().isPaused());
        data.addProperty("render_suppressed",BackgroundRun.renderingSuppressed());
        data.addProperty("client_fps",Minecraft.getInstance().getFps());
        data.addProperty("client_tps",Math.round(clientTps*10)/10.0);
        data.addProperty("auto_respawn",BridgeConfig.AUTO_RESPAWN.get());
        data.addProperty("sky_visible",player.level().canSeeSky(player.blockPosition()));
        data.addProperty("on_fire",player.isOnFire());data.addProperty("in_water",player.isInWater());data.addProperty("air",player.getAirSupply());
        data.addProperty("block_light",player.level().getBrightness(net.minecraft.world.level.LightLayer.BLOCK,player.blockPosition()));
        Runtime runtime=Runtime.getRuntime();data.addProperty("heap_used_mb",(runtime.totalMemory()-runtime.freeMemory())/1048576);data.addProperty("heap_max_mb",runtime.maxMemory()/1048576);
        data.addProperty("screen", Minecraft.getInstance().screen == null ? "none" : Minecraft.getInstance().screen.getClass().getSimpleName());
        data.addProperty("health", player.getHealth());
        data.addProperty("max_health", player.getMaxHealth());
        data.addProperty("absorption", player.getAbsorptionAmount());
        data.addProperty("food", player.getFoodData().getFoodLevel());
        data.addProperty("saturation", player.getFoodData().getSaturationLevel());
        data.addProperty("armor", player.getArmorValue());
        data.addProperty("held", player.getMainHandItem().getHoverName().getString());

        JsonArray inventory = new JsonArray();
        JsonArray items = new JsonArray();
        for (int i = 0; i < player.getInventory().getContainerSize(); i++) {
            ItemStack stack = player.getInventory().getItem(i);
            if (!stack.isEmpty()) {
                if (inventory.size() < 12) inventory.add(stack.getHoverName().getString() + " x" + stack.getCount());
                JsonObject item = SurvivalActions.stackInfo(stack);
                item.addProperty("slot", i);
                item.addProperty("count", stack.getCount());
                items.add(item);
            }
        }
        data.add("inventory", inventory);
        data.add("inventory_items", items);
        data.addProperty("inventory_complete", true);

        List<Entity> nearby = new ArrayList<>(
                player.level().getEntities(player, player.getBoundingBox().inflate(16.0), e -> e != player));
        nearby.sort(Comparator.comparingDouble((Entity e) -> e.distanceToSqr(player)));
        JsonArray players = new JsonArray();
        JsonArray entities = new JsonArray();
        JsonArray hostiles = new JsonArray();
        for (Entity entity : nearby) {
            if (entity instanceof Player other) {
                if (players.size() < 8) {
                    players.add(other.getGameProfile().getName());
                }
            } else {
                if (entity instanceof LivingEntity living
                        && entity.getType().getCategory() == MobCategory.MONSTER
                        && hostiles.size() < 8) {
                    JsonObject hostile = new JsonObject();
                    hostile.addProperty("name", living.getName().getString());
                    hostile.addProperty("type", BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).toString());
                    hostile.addProperty("line_of_sight",player.hasLineOfSight(living));
                    hostile.addProperty("health", living.getHealth());
                    hostile.addProperty("distance", living.distanceTo(player));
                    JsonObject hp = new JsonObject();
                    hp.addProperty("x", living.getX());
                    hp.addProperty("y", living.getY());
                    hp.addProperty("z", living.getZ());
                    hostile.add("position", hp);
                    hostiles.add(hostile);
                }
                if (entities.size() < 8) {
                    entities.add(BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).toString());
                }
            }
        }
        data.add("nearby_players", players);
        data.add("nearby_entities", entities);
        data.add("hostiles", hostiles);
        return data;
    }

    private LivingEntity findHostile(LocalPlayer player, String name) {
        String wanted = name == null ? "" : name.trim().toLowerCase();
        List<Entity> candidates = new ArrayList<>(
                player.level().getEntities(
                        player,
                        player.getBoundingBox().inflate(6.0),
                        e -> e instanceof LivingEntity
                                && !(e instanceof Player)
                                && e.getType().getCategory() == MobCategory.MONSTER));
        candidates.sort(Comparator.comparingDouble(e -> e.distanceToSqr(player)));
        for (Entity entity : candidates) {
            if (!(entity instanceof LivingEntity living)) {
                continue;
            }
            if (wanted.isEmpty()
                    || living.getName().getString().toLowerCase().contains(wanted)
                    || living.getType().toString().toLowerCase().contains(wanted)
                    || BuiltInRegistries.ENTITY_TYPE.getKey(living.getType()).toString().toLowerCase().contains(wanted)) {
                return living;
            }
        }
        return null;
    }

    private void attack(String id, LocalPlayer player, String name) {
        LivingEntity target = findHostile(player, name);
        if (target == null || target.distanceTo(player) > 3.5F || !player.hasLineOfSight(target)) {
            sendResult(id, false, "没有可触及的敌对生物", null);
            return;
        }
        Minecraft mc = Minecraft.getInstance();
        if (mc.gameMode == null) {
            sendResult(id, false, "game mode unavailable", null);
            return;
        }
        player.lookAt(EntityAnchorArgument.Anchor.EYES, target.getEyePosition());
        mc.gameMode.attack(player, target);
        player.swing(InteractionHand.MAIN_HAND);
        WebSocket origin = socket;
        CompletableFuture.delayedExecutor(250, TimeUnit.MILLISECONDS).execute(() -> mc.execute(() -> {
            if (socket != origin) return;
            JsonObject data = new JsonObject();
            data.addProperty("target", target.getName().getString());
            data.addProperty("health", target.getHealth());
            data.addProperty("distance", target.distanceTo(player));
            data.addProperty("dead", target.isDeadOrDying());
            data.addProperty("phase", "attempted");
            sendResult(id, true, null, data);
        }));
    }

    private void eat(String id, LocalPlayer player) {
        int slot = -1;
        int nutrition = -1;
        String itemName = "";
        for (int i = 0; i < 9; i++) {
            ItemStack stack = player.getInventory().getItem(i);
            var food = stack.get(DataComponents.FOOD);
            if (!stack.isEmpty() && food != null && food.effects().isEmpty() && food.nutrition() > nutrition) {
                slot = i;
                nutrition = food.nutrition();
                itemName = stack.getHoverName().getString();
            }
        }
        if (slot < 0) {
            sendResult(id, false, "快捷栏里没有食物，请先把食物放到 1-9 格", null);
            return;
        }
        Minecraft mc = Minecraft.getInstance();
        if(mc.screen!=null||mc.gameMode==null||!player.getFoodData().needsFood()) {
            sendResult(id,false,"请先关闭界面；饱食已满时不额外进食",null);return;
        }
        player.getInventory().selected=slot;
        player.connection.send(new net.minecraft.network.protocol.game.ServerboundSetCarriedItemPacket(slot));
        var food=player.getInventory().getItem(slot).get(DataComponents.FOOD);
        int eatMs=Math.max(400,Math.min(10000,(int)Math.ceil(food.eatSeconds()*1000)));
        eatingGeneration=actionGeneration.incrementAndGet();eatingOrigin=socket;eatingFoodBefore=player.getFoodData().getFoodLevel();eatingStarted=System.currentTimeMillis();eatingDeadline=eatingStarted+eatMs+1500;
        // Use the item directly: ray-based right click could open the workbench we just used.
        mc.options.keyUse.setDown(true);mc.gameMode.useItem(player,InteractionHand.MAIN_HAND);
        if(!player.isUsingItem()){eatingGeneration=-1;mc.options.keyUse.setDown(false);sendResult(id,false,"客户端未开始进食",null);return;}
        JsonObject data = new JsonObject();
        data.addProperty("item", itemName);
        data.addProperty("slot", slot + 1);
        data.addProperty("eat_ms",eatMs);
        sendResult(id, true, null, data);
    }

    private void look(String id, LocalPlayer player, String name) {
        String wanted = name == null ? "" : name.trim();
        if (wanted.isEmpty()) {
            sendResult(id, false, "missing player", null);
            return;
        }
        Entity target = player.level()
                .getEntities(
                        player,
                        player.getBoundingBox().inflate(64.0),
                        e -> e instanceof Player && e != player
                                && e.getName().getString().equalsIgnoreCase(wanted))
                .stream()
                .findFirst()
                .orElse(null);
        if (target == null) {
            sendResult(id, false, "player not found: " + wanted, null);
            return;
        }
        double dx = target.getX() - player.getX();
        double dy = target.getEyeY() - player.getEyeY();
        double dz = target.getZ() - player.getZ();
        double horizontal = Math.sqrt(dx * dx + dz * dz);
        float yaw = (float) (Math.toDegrees(Math.atan2(dz, dx))) - 90.0F;
        float pitch = (float) (-Math.toDegrees(Math.atan2(dy, horizontal)));
        player.setYRot(yaw);
        player.setXRot(pitch);
        player.setYHeadRot(yaw);
        JsonObject data = new JsonObject();
        data.addProperty("yaw", yaw);
        data.addProperty("pitch", pitch);
        sendResult(id, true, null, data);
    }

    private void come(String id, LocalPlayer player, String name) throws ReflectiveOperationException {
        String wanted = name == null ? "" : name.trim();
        if (wanted.isEmpty()) {
            sendResult(id, false, "missing player", null);
            return;
        }
        Player target = player.level()
                .getEntities(
                        player,
                        player.getBoundingBox().inflate(64.0),
                        e -> e instanceof Player other
                                && other != player
                                && other.getName().getString().equalsIgnoreCase(wanted))
                .stream()
                .map(e -> (Player) e)
                .findFirst()
                .orElse(null);
        if (target == null) {
            sendResult(id, false, "player not found within 64 blocks: " + wanted, null);
            return;
        }
        long x = (long) Math.floor(target.getX());
        long y = (long) Math.floor(target.getY());
        long z = (long) Math.floor(target.getZ());
        actionGeneration.incrementAndGet();
        Minecraft.getInstance().options.keyUse.setDown(false);
        survival.cancel("new walk-to-player request");survival.prepareNavigation();
        player.connection.sendChat("#goto " + x + " " + y + " " + z);
        JsonObject data = new JsonObject();
        data.addProperty("target", target.getName().getString());
        data.addProperty("x", x);
        data.addProperty("y", y);
        data.addProperty("z", z);
        data.addProperty("distance", target.distanceTo(player));
        sendResult(id, true, null, data);
    }

    private void screenshot(String id) {
        if(pendingCapture!=null){sendResult(id,false,"another screenshot is pending",null);return;}
        pendingCapture=id;captureOrigin=socket;captureDeadline=System.currentTimeMillis()+4000;
        BackgroundRun.requestFrame();
    }

    public void afterFrame() {
        if(pendingCapture==null||socket!=captureOrigin||Minecraft.getInstance().player==null)return;
        String id=pendingCapture;pendingCapture=null;
        Minecraft mc = Minecraft.getInstance();
        NativeImage image = null;
        try {
            image = Screenshot.takeScreenshot(mc.getMainRenderTarget());
            if (image == null) {
                sendResult(id, false, "screenshot returned null", null);
                return;
            }
            if(image.getWidth()<320||image.getHeight()<180) {
                pendingCapture=id;BackgroundRun.requestFrame();return;
            }
            JsonObject data = new JsonObject();
            data.addProperty("format", "png");
            data.addProperty("width", image.getWidth());
            data.addProperty("height", image.getHeight());
            data.addProperty("base64", Base64.getEncoder().encodeToString(image.asByteArray()));
            data.add("observation",GameSense.observe(mc.player));
            data.addProperty("fresh_frame",true);
            sendResult(id, true, null, data);
        } catch (Exception exc) {
            sendResult(id, false, "screenshot failed: " + exc, null);
        } finally {
            if (image != null) {
                image.close();
            }
        }
    }

    public void onGameChat(ClientChatReceivedEvent event) {
        if (!started || socket == null || !forwardChat || !(event instanceof ClientChatReceivedEvent.Player chat)) return;
        Minecraft mc = Minecraft.getInstance();
        if (event.isSystem() || mc.getConnection() == null) return;
        var info = mc.getConnection().getPlayerInfo(event.getSender());
        if (info == null) return;
        String body = chat.getPlayerChatMessage().signedContent();
        if (body.isBlank() || body.length() > 1000) return;
        JsonObject data = new JsonObject();
        data.addProperty("kind", "player");
        data.addProperty("sender_uuid", event.getSender().toString());
        data.addProperty("sender_name", info.getProfile().getName());
        data.addProperty("body", body);
        JsonObject envelope = new JsonObject();
        envelope.addProperty("type", "event");
        envelope.addProperty("event", "chat");
        envelope.add("data", data);
        send(envelope);
    }

    private void sendResult(String id, boolean ok, String error, JsonObject data) {
        JsonObject result = new JsonObject();
        result.addProperty("type", "result");
        result.addProperty("id", id);
        result.addProperty("ok", ok);
        if (ok && data != null) {
            result.add("data", data);
        } else if (!ok) {
            result.addProperty("error", error == null ? "unknown error" : error);
        }
        send(result);
    }

    private void send(JsonObject obj) {
        WebSocket ws = socket;
        if (ws == null || ws.isOutputClosed()) return;
        String payload = obj.toString();
        synchronized (sendLock) {
            sendTail = sendTail.handle((ignored, previousError) -> null).thenCompose(ignored -> {
                if (socket != ws || ws.isOutputClosed()) return CompletableFuture.completedFuture(null);
                return ws.sendText(payload, true).thenApply(sent -> (Void) null);
            }).exceptionally(error -> {
                AstrBotBridge.LOGGER.warn("[astrbot-bridge] send failed: {}", error.getClass().getSimpleName());
                disconnect(ws);
                ws.abort();
                return null;
            });
        }
    }

    private void disconnect(WebSocket origin) {
        if (socket != origin) return;
        socket = null;
        actionGeneration.incrementAndGet();
        scheduleReconnect();
        Minecraft mc = Minecraft.getInstance();
        mc.execute(() -> {
            if (socket != null) return;
            survival.cancel("bridge disconnected");
            mc.options.keyUse.setDown(false);
            if (mc.player != null) mc.player.connection.sendChat("#stop");
        });
    }

    static boolean validBaritone(String command) {
        if (command.equals("stop")) return true;
        if (command.matches("goto -?[0-9]{1,8} -?[0-9]{1,4} -?[0-9]{1,8}")) return true;
        if (command.matches("follow player [A-Za-z0-9_]{1,16}")) return true;
        if (command.matches("mine [1-9][0-9]{0,4}(?: [a-z0-9_]+(?::[a-z0-9_]+)?)+")) {
            return Integer.parseInt(command.split(" ")[1]) <= 16384;
        }
        return false;
    }

    private static JsonObject simple(String key, boolean value) {
        JsonObject obj = new JsonObject();
        obj.addProperty(key, value);
        return obj;
    }

    private static String optString(JsonObject obj, String key, String fallback) {
        if (obj == null || !obj.has(key) || obj.get(key).isJsonNull()) {
            return fallback;
        }
        JsonElement value = obj.get(key);
        return value.isJsonPrimitive() ? value.getAsString() : fallback;
    }
}
