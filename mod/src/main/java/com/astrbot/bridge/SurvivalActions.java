package com.astrbot.bridge;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.HashSet;
import java.util.function.LongSupplier;
import java.util.function.Supplier;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.screens.inventory.AbstractContainerScreen;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.commands.arguments.EntityAnchorArgument;
import net.minecraft.core.BlockPos;
import net.minecraft.core.Direction;
import net.minecraft.core.component.DataComponents;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.core.registries.Registries;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.network.protocol.game.ServerboundPlayerActionPacket;
import net.minecraft.network.protocol.game.ServerboundPlayerCommandPacket;
import net.minecraft.network.protocol.game.ServerboundPlayerInputPacket;
import net.minecraft.network.protocol.game.ServerboundSelectTradePacket;
import net.minecraft.network.protocol.game.ServerboundSetCarriedItemPacket;
import net.minecraft.tags.BlockTags;
import net.minecraft.tags.TagKey;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.EquipmentSlot;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.monster.Enemy;
import net.minecraft.world.entity.item.ItemEntity;
import net.minecraft.world.entity.player.Inventory;
import net.minecraft.world.inventory.AbstractContainerMenu;
import net.minecraft.world.inventory.ClickType;
import net.minecraft.world.inventory.CraftingMenu;
import net.minecraft.world.inventory.InventoryMenu;
import net.minecraft.world.inventory.FurnaceMenu;
import net.minecraft.world.inventory.MerchantMenu;
import net.minecraft.world.inventory.Slot;
import net.minecraft.world.item.ArmorItem;
import net.minecraft.world.item.BlockItem;
import net.minecraft.world.item.HoeItem;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;
import net.minecraft.world.item.crafting.CraftingRecipe;
import net.minecraft.world.item.crafting.ShapedRecipe;
import net.minecraft.world.item.crafting.ShapelessRecipe;
import net.minecraft.world.item.crafting.AbstractCookingRecipe;
import net.minecraft.world.item.crafting.RecipeType;
import net.minecraft.world.item.crafting.RecipeHolder;
import net.minecraft.world.level.ClipContext;
import net.minecraft.world.level.Level;
import net.minecraft.world.level.BlockGetter;
import net.minecraft.world.level.block.BedBlock;
import net.minecraft.world.level.block.ButtonBlock;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.CropBlock;
import net.minecraft.world.level.block.LadderBlock;
import net.minecraft.world.level.block.LeavesBlock;
import net.minecraft.world.level.block.DoorBlock;
import net.minecraft.world.level.block.FallingBlock;
import net.minecraft.world.level.block.FenceGateBlock;
import net.minecraft.world.level.block.LeverBlock;
import net.minecraft.world.level.block.TrapDoorBlock;
import net.minecraft.world.level.block.entity.BlockEntity;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.block.state.properties.DoubleBlockHalf;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.HitResult;
import net.minecraft.world.phys.Vec3;
import net.minecraft.world.phys.AABB;
import net.minecraft.world.phys.shapes.Shapes;
import net.minecraft.world.phys.shapes.VoxelShape;

/** Server-authoritative inventory and block interactions. Long work is tick-driven and cancellable. */
final class SurvivalActions {
    interface Reply { void send(String id, boolean ok, String error, JsonObject data); }
    private final Reply reply;
    private final Supplier<java.net.http.WebSocket> connection;
    private final LongSupplier generation;
    private final SafeNavigation navigation = new SafeNavigation();
    private final NavigationFeedback pathFeedback=new NavigationFeedback();
    private final LinkedHashMap<String,JsonObject> history = new LinkedHashMap<>();
    private Job job;
    private BlockPos navigationTarget;
    private LocalPlayer navigationActor;
    private Level navigationWorld;
    private java.net.http.WebSocket navigationConnection;
    private long navigationGeneration,navigationStillSince;
    private Vec3 navigationLastPosition;
    private boolean breakBudgetActive, breakBudgetExhausted;
    private int breakBudgetCount, breakBudgetLimit;
    private Map<BlockPos, Boolean> breakBudgetSnapshot = new HashMap<>();
    private static final Set<String> STONES = Set.of("minecraft:stone","minecraft:deepslate");
    private static final Set<String> COAL = Set.of("minecraft:coal_ore","minecraft:deepslate_coal_ore");
    private static final Set<String> IRON = Set.of("minecraft:iron_ore","minecraft:deepslate_iron_ore");
    private static final Set<String> SOIL = Set.of("minecraft:dirt","minecraft:grass_block","minecraft:podzol","minecraft:rooted_dirt","minecraft:coarse_dirt","minecraft:moss_block");

    private static final class Job {
        String id, kind, item="";
        long generation, started=System.currentTimeMillis(), next, deadline, stageDeadline;
        java.net.http.WebSocket origin;
        int stage, baseline, count, menu=-1, completed;
        BlockPos pos;
        BlockState oldBlock;
        Level world;
        RecipeHolder<?> recipe;
        Direction face=Direction.UP;
        float progress;
        boolean collectOnly;
        boolean recoverStation;
        boolean manualRecipe;
        ItemEntity drop;
        Vec3 originPosition;
        JsonObject detail=new JsonObject();
    }

    SurvivalActions(Reply reply, Supplier<java.net.http.WebSocket> connection, LongSupplier generation) {
        this.reply=reply; this.connection=connection; this.generation=generation;
    }
    static boolean handles(String action) {
        return Set.of("inventory","equip","unequip_armor","craft","workbench","sleep","scan","scan_blocks","scan_entities","inspect_block","block_context","get_menu","mine_block","mine_at","mine_below","place_at","use_item","use_bonemeal","release_use","interact_block","drop_item","select_slot","container","sort_inventory","move_item","trade","ride","dismount","wake","plant","till","dig_down","jump","align","operation","navigate","station","smelt","interact","pickup","ladder_assist").contains(action);
    }
    boolean busy() { return job!=null; }
    void cancel(String reason) {
        navigationTarget=null;
        resetBreakBudget();
        pathFeedback.end();
        Minecraft mc=Minecraft.getInstance();
        if(mc.gameMode!=null) mc.gameMode.stopDestroyBlock();
        mc.options.keyUse.setDown(false);
        mc.options.keyAttack.setDown(false);
        if(job!=null) finish(false,reason);
        if(mc.player!=null)mc.player.connection.sendChat("#stop");
        navigation.restore();
    }
    void prepareNavigation() throws ReflectiveOperationException {navigation.begin();}
    JsonObject navigationState(){
        JsonObject result=navigation.state();
        result.addProperty("break_monitoring",breakBudgetActive);
        result.addProperty("break_count",breakBudgetCount);
        result.addProperty("break_limit",breakBudgetLimit);
        result.addProperty("break_budget_exhausted",breakBudgetExhausted);
        return result;
    }
    JsonObject pathState(){return pathFeedback.state();}
    private void beginBreakBudget(int limit) {
        breakBudgetLimit=Math.max(1,Math.min(64,limit));
        breakBudgetCount=0;
        breakBudgetExhausted=false;
        breakBudgetActive=true;
        breakBudgetSnapshot=new HashMap<>();
    }
    private void resetBreakBudget() {
        breakBudgetActive=false;
        breakBudgetExhausted=false;
        breakBudgetCount=0;
        breakBudgetLimit=0;
        breakBudgetSnapshot=new HashMap<>();
    }
    static int breakTransition(Map<BlockPos,Boolean> snapshot,BlockPos pos,boolean solid) {
        Boolean was=snapshot.put(pos,solid);
        return was!=null&&was&&!solid?1:0;
    }
    private void tickBreakBudget() {
        if(!breakBudgetActive)return;
        Minecraft mc=Minecraft.getInstance();LocalPlayer player=mc.player;
        if(player==null||player.level()!=navigationWorld||navigationTarget==null||player.isDeadOrDying()){resetBreakBudget();return;}
        BlockPos center=player.blockPosition();
        Map<BlockPos,Boolean> next=new HashMap<>(640);
        int count=breakBudgetCount;
        for(int dx=-4;dx<=4;dx++)for(int dy=-2;dy<=2;dy++)for(int dz=-4;dz<=4;dz++) {
            BlockPos pos=center.offset(dx,dy,dz);
            boolean solid=!player.level().getBlockState(pos).isAir();
            count+=breakTransition(breakBudgetSnapshot,pos,solid);
            next.put(pos,solid);
        }
        breakBudgetSnapshot=next;breakBudgetCount=count;
        if(count>=breakBudgetLimit) {
            breakBudgetExhausted=true;breakBudgetActive=false;navigationTarget=null;
            pathFeedback.end();
            navigation.restore();
            if(mc.gameMode!=null)mc.gameMode.stopDestroyBlock();
            if(player.connection!=null)player.connection.sendChat("#stop");
            AstrBotBridge.LOGGER.info("[astrbot-bridge] navigation break budget exhausted after {} blocks",count);
        }
    }
    void observeNavigation(BlockPos target){
        observeNavigation(target,false);
    }
    private void observeNavigation(BlockPos target,boolean finishStep){
        pathFeedback.begin(target.getX(),target.getY(),target.getZ());
        navigationTarget=finishStep?target.immutable():null;navigationActor=Minecraft.getInstance().player;navigationWorld=Minecraft.getInstance().level;
        navigationConnection=connection.get();navigationGeneration=generation.getAsLong();navigationLastPosition=null;navigationStillSince=System.currentTimeMillis();
    }

    private void finishNavigationTick(){
        if(navigationTarget==null||job!=null)return;
        Minecraft mc=Minecraft.getInstance();LocalPlayer player=mc.player;
        if(player==null||player!=navigationActor||player.level()!=navigationWorld||generation.getAsLong()!=navigationGeneration
                ||connection.get()!=navigationConnection||mc.screen!=null||player.isDeadOrDying()||player.isSleeping()) {navigationTarget=null;return;}
        if(player.getHealth()<=8||player.isOnFire()||player.isInWater()){navigationTarget=null;return;}
        long now=System.currentTimeMillis();Vec3 pos=player.position();
        if(navigationLastPosition==null||pos.distanceToSqr(navigationLastPosition)>.0004){navigationLastPosition=pos;navigationStillSince=now;return;}
        if(now-navigationStillSince<500||!player.onGround())return;
        double feet=TerrainQueries.height(player,navigationTarget,Set.of());
        if(!Double.isFinite(feet)||Math.abs(player.getY()-feet)>.65||Math.hypot(player.getX()-navigationTarget.getX()-.5,player.getZ()-navigationTarget.getZ()-.5)>1.2)return;
        BlockPos target=navigationTarget;navigationTarget=null;
        JsonObject params=xyz(target);
        try{
            // Baritone may stop at the edge of the last cell. One bounded, collision-proven
            // ordinary sneak step completes it; the caller still verifies actual coordinates.
            localBegin(player,"align",params);player.connection.sendChat("#stop");
        }catch(RuntimeException unavailable){/* Keep the caller's original bounded failure path. */}
    }

    JsonObject localScan(LocalPlayer player,JsonObject params){return scan(player,params,16);}
    boolean localPassable(LocalPlayer player,BlockPos pos,Set<BlockPos> ignored){return TerrainQueries.passable(player.level(),pos,ignored);}
    boolean localStandable(LocalPlayer player,BlockPos pos,Set<BlockPos> ignored){return Double.isFinite(TerrainQueries.height(player,pos,ignored));}
    boolean localClearable(LocalPlayer player,BlockPos pos){return pos.getY()>=player.blockPosition().getY()&&natural(player,pos,"leaves")&&interactable(player,pos)&&SurvivalRules.safeMiningCell(pos.getX(),pos.getY(),pos.getZ(),player.getX(),player.getY(),player.getZ());}
    String localBegin(LocalPlayer player,String action,JsonObject p){
        if(job!=null||!Set.of("mine_block","pickup","craft","workbench","smelt","align").contains(action))throw new IllegalStateException("local child action unavailable");
        if(player.isDeadOrDying()||player.isSleeping()||player.isCreative()||player.isSpectator())throw new IllegalStateException("survival player unavailable");
        Job j=new Job();j.id=java.util.UUID.randomUUID().toString();j.kind=action;j.generation=generation.getAsLong();j.origin=connection.get();j.world=player.level();j.deadline=j.started+26000;
        switch(action){case "mine_block"->beginMine(j,player,p);case "pickup"->beginPickup(j,player,p);case "craft"->beginCraft(j,player,p);case "workbench"->beginWorkbench(j,player,p);case "smelt"->beginSmelt(j,player,p);case "align"->beginAlign(j,player,p);default->throw new IllegalArgumentException("unknown child");}
        job=j;return j.id;
    }
    JsonObject localOperation(String id){
        JsonObject value=job!=null&&job.id.equals(id)?progress(job,"running"):history.get(id);
        if(value==null)throw new IllegalStateException("local child operation unknown");return value.deepCopy();
    }
    void localPrepareProduction(LocalPlayer player){normalInventory(player);}

    void handle(String id,String action,JsonObject p,LocalPlayer player) {
        try {
            if(action.equals("operation")) {
                String key=text(p,"operation_id","");
                JsonObject result=job!=null && job.id.equals(key) ? progress(job,"running") : history.get(key);
                if(result==null) throw new IllegalArgumentException("operation is unknown or expired");
                reply.send(id,true,null,result.deepCopy()); return;
            }
            if(action.equals("scan")) { reply.send(id,true,null,scan(player,p)); return; }
            if(action.equals("inventory") && text(p,"operation","view").equals("view")) {
                reply.send(id,true,null,inventory(player)); return;
            }
            if(action.equals("scan_blocks")) { reply.send(id,true,null,scanBlocks(player,p)); return; }
            if(action.equals("scan_entities")) { reply.send(id,true,null,scanEntities(player,p)); return; }
            if(action.equals("inspect_block")) { reply.send(id,true,null,inspectBlock(player,p)); return; }
            if(action.equals("block_context")) { reply.send(id,true,null,blockContext(player,p)); return; }
            if(action.equals("get_menu")) { reply.send(id,true,null,getMenu(player)); return; }
            if(action.equals("drop_item")) { if(job!=null) throw new IllegalStateException("another survival action is running; stop it first"); reply.send(id,true,null,dropItem(player,p)); return; }
            if(action.equals("select_slot")) { if(job!=null) throw new IllegalStateException("another survival action is running; stop it first"); reply.send(id,true,null,selectSlot(player,p)); return; }
            if(action.equals("sort_inventory")) { if(job!=null) throw new IllegalStateException("another survival action is running; stop it first"); reply.send(id,true,null,sortInventory(player)); return; }
            if(action.equals("container")) { reply.send(id,true,null,container(player,p)); return; }
            if(action.equals("use_item")) {
                if(job!=null) throw new IllegalStateException("another survival action is running; stop it first");
                reply.send(id,true,null,useItem(player,p)); return;
            }
            if(action.equals("release_use")) {
                player.stopUsingItem();
                player.connection.send(new ServerboundPlayerActionPacket(ServerboundPlayerActionPacket.Action.RELEASE_USE_ITEM,BlockPos.ZERO,Direction.DOWN));
                JsonObject released=new JsonObject();released.addProperty("released",true);reply.send(id,true,null,released);return;
            }
            if(action.equals("interact_block")) {
                if(job!=null) throw new IllegalStateException("another survival action is running; stop it first");
                reply.send(id,true,null,interactBlock(player,p)); return;
            }
            if(action.equals("move_item")) {
                if(job!=null) throw new IllegalStateException("another survival action is running; stop it first");
                reply.send(id,true,null,moveItem(player,p)); return;
            }
            if(action.equals("trade")) {
                if(job!=null&&!text(p,"operation","read").equals("read")) throw new IllegalStateException("another survival action is running; stop it first");
                reply.send(id,true,null,trade(player,p)); return;
            }
            if(action.equals("ride")) {
                if(job!=null) throw new IllegalStateException("another survival action is running; stop it first");
                reply.send(id,true,null,ride(player,p)); return;
            }
            if(action.equals("wake")) {
                reply.send(id,true,null,wake(player)); return;
            }
            if(job!=null) throw new IllegalStateException("another survival action is running; stop it first");
            if(player.isDeadOrDying() || player.isSleeping()) throw new IllegalStateException("player is dead or sleeping");
            Minecraft mc=Minecraft.getInstance();
            if(mc.gameMode==null || player.isSpectator() || player.isCreative()) throw new IllegalStateException("survival game mode unavailable");
            if(action.equals("navigate")) {
                BlockPos target=position(p);
                if(!SurvivalRules.within(target.getX(),target.getY(),target.getZ(),player.getX(),player.getY(),player.getZ(),32))
                    throw new IllegalArgumentException("safe navigation is limited to 32 blocks, 12 vertically");
                boolean allowBreak=p.has("allow_break")&&p.get("allow_break").getAsBoolean();
                int maxBreak=allowBreak?Math.max(1,Math.min(64,integer(p,"max_break",8))):0;
                navigation.begin(allowBreak);
                observeNavigation(target,true);
                if(allowBreak)beginBreakBudget(maxBreak);else resetBreakBudget();
                player.connection.sendChat("#goto "+target.getX()+" "+target.getY()+" "+target.getZ());
                JsonObject result=new JsonObject();result.addProperty("phase","accepted");result.addProperty("allow_break",allowBreak);result.addProperty("max_break",maxBreak);reply.send(id,true,null,result);return;
            }
            Job j=new Job();j.id=id;j.kind=action;j.generation=generation.getAsLong();j.origin=connection.get();j.world=player.level();j.deadline=j.started+26000;
            switch(action) {
                case "inventory" -> beginInventory(j,player,p);
                case "equip" -> beginEquip(j,player,p);
                case "unequip_armor" -> beginUnequipArmor(j,player,p);
                case "craft" -> beginCraft(j,player,p);
                case "workbench" -> beginWorkbench(j,player,p);
                case "sleep" -> beginSleep(j,player,p);
                case "mine_block" -> beginMine(j,player,p);
                case "mine_at" -> beginMineAt(j,player,p);
                case "mine_below" -> beginMineBelow(j,player);
                case "dig_down" -> beginDigDown(j,player,p);
                case "align" -> beginAlign(j,player,p);
                case "ladder_assist" -> beginLadderAssist(j,player,p);
                case "place_at" -> beginPlaceAt(j,player,p);
                case "station" -> beginStation(j,player,p);
                case "smelt" -> beginSmelt(j,player,p);
                case "interact" -> beginInteract(j,player,p);
                case "pickup" -> beginPickup(j,player,p);
                case "plant" -> beginPlant(j,player,p);
                case "till" -> beginTill(j,player,p);
                case "use_bonemeal" -> beginBonemeal(j,player,p);
                case "jump" -> beginJump(j,player);
                case "dismount" -> beginDismount(j,player);
                default -> throw new IllegalArgumentException("unsupported survival action");
            }
            job=j;
            JsonObject result=progress(j,"started");result.addProperty("operation_id",j.id);reply.send(id,true,null,result);
        } catch(Exception ex) { reply.send(id,false,ex.getMessage(),null); }
    }

    void tick() {
        try{navigation.enforce();}catch(ReflectiveOperationException ex){cancel("navigation protection unavailable");return;}
        if(job==null) {
            try{finishNavigationTick();}catch(RuntimeException unavailable){navigationTarget=null;}
            tickBreakBudget();
        }
        if(job==null) return;
        Minecraft mc=Minecraft.getInstance();Job j=job;long now=System.currentTimeMillis();
        if(mc.player==null || j.origin!=connection.get() || j.generation!=generation.getAsLong()) {cancel("action cancelled by stop/disconnect");return;}
        if(mc.player.isDeadOrDying()) {cancel("player died");return;}
        if(mc.player.level()!=j.world) {cancel("world changed");return;}
        if(now>j.deadline) {cancel("action timed out; completion not confirmed");return;}
        if(now<j.next) return;
        try {
            switch(j.kind) {
                case "inventory","equip" -> verifyInventory(j,mc.player);
                case "unequip_armor" -> verifyUnequipArmor(j,mc.player);
                case "craft" -> tickCraft(j,mc.player,now);
                case "workbench" -> tickWorkbench(j,mc.player);
                case "sleep" -> { if(mc.player.isSleeping()) {j.detail.addProperty("sleeping",true);finish(true,null);} }
                case "mine_block" -> tickMine(j,mc.player,now);
                case "mine_at" -> tickMine(j,mc.player,now);
                case "place_at" -> tickPlaceAt(j,mc.player,now);
                case "station" -> {
                    if(BuiltInRegistries.BLOCK.getKey(mc.player.level().getBlockState(j.pos).getBlock()).toString().equals(j.item)) {
                        j.detail.addProperty("placed",true);finish(true,null);
                    }
                }
                case "recover_station" -> {
                    if(j.stage==0) {
                        if(!(mc.player.containerMenu instanceof FurnaceMenu menu))break;
                        if(!menu.getCarried().isEmpty()||menu.isLit())throw new IllegalStateException("station is burning or cursor occupied; recovery refused");
                        for(int i=0;i<3;i++)if(menu.getSlot(i).hasItem())throw new IllegalStateException("station contains items; recovery refused");
                        mc.player.closeContainer();j.stage=1;j.next=now+700;
                    } else {
                        j.recoverStation=true;j.kind="mine_block";JsonObject p=new JsonObject();p.addProperty("x",j.pos.getX());p.addProperty("y",j.pos.getY());p.addProperty("z",j.pos.getZ());p.addProperty("resource",j.item);beginMine(j,mc.player,p);
                    }
                }
                case "smelt" -> tickSmelt(j,mc.player,now);
                case "pickup" -> tickPickup(j,mc.player,now);
                case "plant" -> tickPlant(j,mc.player,now);
                case "till" -> tickTill(j,mc.player,now);
                case "use_bonemeal" -> tickBonemeal(j,mc.player,now);
                case "jump" -> tickJump(j,mc.player,now);
                case "dig_down" -> tickDigDown(j,mc.player,now);
                case "dismount" -> tickDismount(j,mc.player,now);
                case "align" -> tickAlign(j,mc.player,now);
                case "ladder_assist" -> tickLadderAssist(j,mc.player,now);
                case "interact" -> {
                    BlockState state=mc.player.level().getBlockState(j.pos);
                    if(state.getBlock() instanceof DoorBlock&&state.getValue(DoorBlock.OPEN)==(j.count==1)){j.detail.addProperty("open",j.count==1);finish(true,null);}
                }
                default -> throw new IllegalStateException("unknown running job");
            }
        } catch(Exception ex) { finish(false,ex.getMessage()); }
    }

    private JsonObject progress(Job j,String phase) {
        JsonObject r=j.detail.deepCopy();r.addProperty("operation_id",j.id);r.addProperty("kind",j.kind);r.addProperty("phase",phase);return r;
    }
    private void finish(boolean success,String error) {
        Job j=job;if(j==null)return;
        JsonObject result=progress(j,success?"completed":"failed");result.addProperty("success",success);
        if(error!=null)result.addProperty("error",error);
        history.put(j.id,result);while(history.size()>16)history.remove(history.keySet().iterator().next());job=null;
        Minecraft mc=Minecraft.getInstance();
        if(j.kind.equals("pickup")||j.kind.equals("align")||j.kind.equals("ladder_assist")){mc.options.keyUp.setDown(false);mc.options.keyShift.setDown(false);}
        if((j.kind.equals("jump")||j.kind.equals("ladder_assist"))&&mc.player!=null){mc.player.input.jumping=false;mc.options.keyJump.setDown(false);}
        if((j.kind.equals("mine_block")||j.kind.equals("mine_at")) && mc.gameMode!=null){
            mc.options.keyAttack.setDown(false);mc.gameMode.stopDestroyBlock();
            if(mc.player!=null&&mc.player.level()==j.world&&(j.stage==1||j.stage==3))mc.player.connection.send(new ServerboundPlayerActionPacket(ServerboundPlayerActionPacket.Action.ABORT_DESTROY_BLOCK,j.pos,j.face));
            if(mc.level!=null)mc.level.destroyBlockProgress(mc.player==null?0:mc.player.getId(),j.pos,-1);
        }
        if(j.kind.equals("craft") && mc.player!=null && (mc.player.containerMenu instanceof CraftingMenu||mc.player.containerMenu instanceof InventoryMenu))mc.player.closeContainer();
        if((j.kind.equals("smelt")||j.kind.equals("recover_station"))&&mc.player!=null&&mc.player.containerMenu instanceof FurnaceMenu&&mc.player.containerMenu.getCarried().isEmpty())mc.player.closeContainer();
    }

    JsonObject inventory(LocalPlayer player) {
        JsonObject data=new JsonObject();JsonArray items=new JsonArray();int free=0;
        for(int i=0;i<player.getInventory().getContainerSize();i++) {
            ItemStack stack=player.getInventory().getItem(i);
            if(stack.isEmpty()){if(i<36)free++;continue;}
            JsonObject entry=stackInfo(stack);entry.addProperty("slot",i);items.add(entry);
        }
        data.add("items",items);data.addProperty("empty_slots",free);data.addProperty("selected_slot",player.getInventory().selected);
        data.addProperty("cursor_empty",player.containerMenu.getCarried().isEmpty());data.addProperty("menu",player.containerMenu.getClass().getSimpleName());
        return data;
    }

    private JsonObject getMenu(LocalPlayer player) {
        Minecraft mc=Minecraft.getInstance();AbstractContainerMenu menu=player.containerMenu;
        JsonObject result=new JsonObject();
        result.addProperty("screen",mc.screen==null?"none":mc.screen.getClass().getSimpleName());
        result.addProperty("menu",menu.getClass().getSimpleName());
        result.addProperty("window_id",menu.containerId);
        result.addProperty("title",mc.screen instanceof AbstractContainerScreen<?> screen?screen.getTitle().getString():"");
        result.addProperty("selected_slot",player.getInventory().selected);
        result.addProperty("cursor_empty",menu.getCarried().isEmpty());
        JsonObject cursor=stackInfo(menu.getCarried());cursor.addProperty("empty",menu.getCarried().isEmpty());result.add("cursor",cursor);
        JsonArray slots=new JsonArray();
        for(int i=0;i<menu.slots.size();i++) {
            Slot slot=menu.slots.get(i);JsonObject row=stackInfo(slot.getItem());
            row.addProperty("slot",i);row.addProperty("container_slot",slot.getContainerSlot());
            row.addProperty("player_inventory",slot.container instanceof Inventory);
            row.addProperty("empty",slot.getItem().isEmpty());
            slots.add(row);
        }
        result.addProperty("slot_count",slots.size());result.add("slots",slots);
        return result;
    }
    static JsonObject stackInfo(ItemStack stack) {
        JsonObject data=new JsonObject();data.addProperty("id",BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
        data.addProperty("name",stack.getHoverName().getString());data.addProperty("count",stack.getCount());
        var food=stack.get(DataComponents.FOOD);data.addProperty("safe_food",food!=null&&food.effects().isEmpty());
        if(food!=null)data.addProperty("nutrition",food.nutrition());
        if(stack.getItem() instanceof ArmorItem armor){data.addProperty("armor_slot",armor.getEquipmentSlot().getName());data.addProperty("armor_score",armor.getDefense()+armor.getToughness()/4.0);}
        if(stack.isDamageableItem()){data.addProperty("damage",stack.getDamageValue());data.addProperty("max_damage",stack.getMaxDamage());}
        return data;
    }

    private void normalInventory(LocalPlayer player) {
        if(player.containerMenu instanceof CraftingMenu && player.containerMenu.getCarried().isEmpty()) {
            for(int i=1;i<=9;i++)if(!player.containerMenu.getSlot(i).getItem().isEmpty())
                throw new IllegalStateException("craft grid contains your materials; refusing to close it");
            player.closeContainer();
        }
        if(player.containerMenu!=player.inventoryMenu || !player.containerMenu.getCarried().isEmpty())
            throw new IllegalStateException("close external container and clear cursor first; inventory left untouched");
        if(Minecraft.getInstance().screen instanceof net.minecraft.client.gui.screens.inventory.InventoryScreen) {
            for(int i=1;i<=4;i++)if(!player.inventoryMenu.getSlot(i).getItem().isEmpty())throw new IllegalStateException("inventory craft grid contains your materials");
            player.closeContainer();
        }
    }
    private Slot slot(LocalPlayer player,int inventoryIndex) {
        for(Slot s:player.containerMenu.slots)if(s.container==player.getInventory()&&s.getContainerSlot()==inventoryIndex)return s;
        throw new IllegalArgumentException("inventory slot unavailable");
    }
    private void click(LocalPlayer player,int menuSlot,int button,ClickType kind) {
        Minecraft.getInstance().gameMode.handleInventoryMouseClick(player.containerMenu.containerId,menuSlot,button,kind,player);
    }
    private void select(LocalPlayer player,int index) {
        player.getInventory().selected=index;
        player.connection.send(new ServerboundSetCarriedItemPacket(index));
    }
    private int hotbar(LocalPlayer player,int inventoryIndex) {
        if(!SurvivalRules.inventorySlot(inventoryIndex))throw new IllegalArgumentException("source slot must be 0..35");
        if(inventoryIndex<9)return inventoryIndex;
        int destination=8;
        for(int i=0;i<9;i++)if(player.getInventory().getItem(i).isEmpty()){destination=i;break;}
        click(player,slot(player,inventoryIndex).index,destination,ClickType.SWAP);return destination;
    }
    private void beginInventory(Job j,LocalPlayer player,JsonObject p) {
        normalInventory(player);String op=text(p,"operation","view");int source=integer(p,"slot",-1),dest=integer(p,"hotbar",-1);
        if(op.equals("food")) {
            int best=-1,bestNutrition=-1;
            for(int i=0;i<36;i++){ItemStack s=player.getInventory().getItem(i);var f=s.get(DataComponents.FOOD);
                if(!s.isEmpty()&&f!=null&&f.effects().isEmpty()&&f.nutrition()>bestNutrition){best=i;bestNutrition=f.nutrition();}}
            if(best<0)throw new IllegalStateException("背包没有安全食物");
            source=best;dest=hotbar(player,source);
        } else if(op.equals("select")) {
            if(!SurvivalRules.hotbarSlot(dest))throw new IllegalArgumentException("hotbar must be 0..8");
            select(player,dest);source=dest;
        } else if(op.equals("swap")) {
            if(!SurvivalRules.inventorySlot(source)||!SurvivalRules.hotbarSlot(dest))throw new IllegalArgumentException("slot 0..35 and hotbar 0..8 required");
            click(player,slot(player,source).index,dest,ClickType.SWAP);
        } else throw new IllegalArgumentException("inventory operation must be view, swap, select, food");
        j.detail.addProperty("slot",source);j.detail.addProperty("hotbar",dest);
        j.item=BuiltInRegistries.ITEM.getKey(player.getInventory().getItem(dest).getItem()).toString();j.next=j.started+900;j.count=dest;
    }
    private void beginEquip(Job j,LocalPlayer player,JsonObject p) {
        normalInventory(player);String requested=text(p,"item","best");int chosen=-1,armorIndex=-1;double gain=0;
        for(int i=0;i<36;i++) {
            ItemStack s=player.getInventory().getItem(i);if(!(s.getItem() instanceof ArmorItem armor))continue;
            String key=BuiltInRegistries.ITEM.getKey(s.getItem()).toString();
            if(!requested.equals("best")&&!key.equals(SurvivalRules.itemId(requested)))continue;
            int index=36+armor.getEquipmentSlot().getIndex();Slot target=slot(player,index);
            ItemStack old=target.getItem();if(!target.mayPickup(player)||!target.mayPlace(s))continue;
            double oldScore=old.getItem() instanceof ArmorItem a?a.getDefense()+a.getToughness()/4.0:0;
            double delta=armor.getDefense()+armor.getToughness()/4.0-oldScore;
            if(requested.equals("best")&&delta<=gain)continue;
            chosen=i;armorIndex=index;gain=delta;if(!requested.equals("best"))break;
        }
        if(chosen<0)throw new IllegalStateException(requested.equals("best")?"没有可升级的可穿戴护甲":"没有找到可穿戴的指定护甲");
        j.item=BuiltInRegistries.ITEM.getKey(player.getInventory().getItem(chosen).getItem()).toString();j.count=armorIndex;
        int from=slot(player,chosen).index,to=slot(player,armorIndex).index;
        click(player,from,0,ClickType.PICKUP);click(player,to,0,ClickType.PICKUP);
        if(!player.containerMenu.getCarried().isEmpty())click(player,from,0,ClickType.PICKUP);
        if(!player.containerMenu.getCarried().isEmpty())throw new IllegalStateException("equipment cursor unexpectedly nonempty; stop and inspect");
        j.detail.addProperty("item",j.item);j.detail.addProperty("equipped_slot",armorIndex);j.next=j.started+900;
    }
    private void beginUnequipArmor(Job j,LocalPlayer player,JsonObject p) {
        normalInventory(player);String slotName=text(p,"slot","head");
        int armorIndex=armorInventoryIndex(slotName);Slot source=slot(player,armorIndex);
        ItemStack stack=source.getItem();
        if(stack.isEmpty())throw new IllegalStateException("armor slot is empty: "+slotName);
        if(!(stack.getItem() instanceof ArmorItem))throw new IllegalStateException("item is not armor: "+slotName);
        j.item=BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();j.count=armorIndex;
        click(player,source.index,0,ClickType.QUICK_MOVE);
        if(!player.containerMenu.getCarried().isEmpty())throw new IllegalStateException("cursor unexpectedly nonempty after unequip");
        j.detail.addProperty("item",j.item);j.detail.addProperty("slot",slotName);j.next=j.started+900;
    }

    private int armorInventoryIndex(String slot) {
        return switch(slot.toLowerCase(java.util.Locale.ROOT)) {
            case "head","helmet","头盔" -> 39;
            case "chest","chestplate","胸甲" -> 38;
            case "legs","leggings","护腿" -> 37;
            case "feet","boots","鞋子","靴子" -> 36;
            default -> throw new IllegalArgumentException("armor slot must be head/chest/legs/feet");
        };
    }

    private void verifyUnequipArmor(Job j,LocalPlayer player) {
        if(!slot(player,j.count).getItem().isEmpty())throw new IllegalStateException("armor slot is still occupied");
        if(find(player,j.item)<0)throw new IllegalStateException("unequipped armor was not found in inventory");
        j.detail.addProperty("unequipped",true);finish(true,null);
    }

    private void verifyInventory(Job j,LocalPlayer player) {
        if(!player.containerMenu.getCarried().isEmpty())throw new IllegalStateException("cursor not empty after operation");
        String actual=BuiltInRegistries.ITEM.getKey(player.getInventory().getItem(j.count).getItem()).toString();
        if(!actual.equals(j.item))throw new IllegalStateException("server did not confirm inventory/equipment change");
        j.detail.addProperty("item",actual);finish(true,null);
    }

    private void beginCraft(Job j,LocalPlayer player,JsonObject p) {
        if(!(player.containerMenu instanceof InventoryMenu||player.containerMenu instanceof CraftingMenu)||!player.containerMenu.getCarried().isEmpty())
            throw new IllegalStateException("craft requires inventory or crafting table and empty cursor");
        for(int i=1;i<=(player.containerMenu instanceof CraftingMenu?9:4);i++)if(!player.containerMenu.getSlot(i).getItem().isEmpty())
            throw new IllegalStateException("craft grid is occupied; refusing to replace your materials");
        j.item=SurvivalRules.itemId(text(p,"item",""));j.count=SurvivalRules.checkedCount(integer(p,"count",1));
        int width=player.containerMenu instanceof CraftingMenu?3:2;
        net.minecraft.world.entity.player.StackedContents available = new net.minecraft.world.entity.player.StackedContents();
        player.getInventory().fillStackedContents(available);
        List<RecipeHolder<?>> matching=player.level().getRecipeManager().getRecipes().stream()
            .filter(r->r.value() instanceof CraftingRecipe)
            .filter(r->BuiltInRegistries.ITEM.getKey(r.value().getResultItem(player.registryAccess()).getItem()).toString().equals(j.item))
            .filter(r->player.getRecipeBook().contains(r)||r.value() instanceof ShapedRecipe||r.value() instanceof ShapelessRecipe).toList();
        if(matching.isEmpty())throw new IllegalStateException("当前服务器没有可执行的普通合成配方");
        boolean fitsNow=matching.stream().anyMatch(r->r.value().canCraftInDimensions(width,width));
        if(!fitsNow) {
            if(matching.stream().anyMatch(r->r.value().canCraftInDimensions(3,3)))throw new IllegalStateException("CRAFT_REQUIRES_TABLE: 这个配方需要工作台");
            throw new IllegalStateException("现有材料不足，未执行合成");
        }
        if(matching.stream().noneMatch(r->available.canCraft(r.value(),null)))throw new IllegalStateException("现有材料不足，未执行合成");
        j.recipe=matching.stream().filter(r->r.value().canCraftInDimensions(width,width)&&available.canCraft(r.value(),null))
            .sorted(Comparator.comparingInt((RecipeHolder<?> r)->player.getRecipeBook().contains(r)?0:1).thenComparing(r->r.id().toString())).findFirst().orElse(null);
        if(j.recipe==null)throw new IllegalStateException("CRAFT_REQUIRES_TABLE: 这个配方需要工作台或材料不足");
        if(player.getInventory().getFreeSlot()<0)throw new IllegalStateException("背包已满，请先腾出空间");
        j.menu=player.containerMenu.containerId;j.baseline=count(player,j.item);j.detail.addProperty("item",j.item);
        j.manualRecipe=!player.getRecipeBook().contains(j.recipe);
        placeCraftRecipe(j,player);j.next=j.started+250;j.stage=0;j.stageDeadline=j.started+2500;
    }
    private void placeCraftRecipe(Job j,LocalPlayer player) {
        if(!j.manualRecipe){Minecraft.getInstance().gameMode.handlePlaceRecipe(j.menu,j.recipe,false);return;}
        int grid=player.containerMenu instanceof CraftingMenu?3:2;
        for(int i=1;i<=grid*grid;i++)if(player.containerMenu.getSlot(i).hasItem())throw new IllegalStateException("manual crafting grid is occupied; refusing to replace materials");
        var ingredients=j.recipe.value().getIngredients();int width=j.recipe.value() instanceof ShapedRecipe shaped?shaped.getWidth():grid;
        var indices=new ArrayList<Integer>();int[] source=new int[ingredients.size()],remaining=new int[36];
        for(int i=0;i<36;i++)remaining[i]=player.getInventory().getItem(i).getCount();
        for(int i=0;i<ingredients.size();i++)if(!ingredients.get(i).isEmpty())indices.add(i);
        indices.sort(Comparator.comparingInt(i->{int options=0;for(int n=0;n<36;n++)if(remaining[n]>0&&ingredients.get(i).test(player.getInventory().getItem(n)))options++;return options;}));
        // Reserve all source slots before touching the grid. Failure leaves materials untouched.
        for(int i:indices){int chosen=-1;for(int n=0;n<36;n++)if(remaining[n]>0&&ingredients.get(i).test(player.getInventory().getItem(n))){chosen=n;break;}if(chosen<0)throw new IllegalStateException("manual recipe materials could not be safely assigned");source[i]=chosen;remaining[chosen]--;}
        for(int i:indices){int from=slot(player,source[i]).index;int to=SurvivalRules.craftSlot(grid,width,i);click(player,from,0,ClickType.PICKUP);click(player,to,1,ClickType.PICKUP);if(!player.containerMenu.getCarried().isEmpty())click(player,from,0,ClickType.PICKUP);if(!player.containerMenu.getCarried().isEmpty())throw new IllegalStateException("manual crafting cursor not empty; inspect before retrying");}
        j.detail.addProperty("placement","manual_server_recipe");
    }
    private void tickCraft(Job j,LocalPlayer player,long now) {
        if(player.containerMenu.containerId!=j.menu)throw new IllegalStateException("crafting menu changed");
        if(j.stage==0) {
            ItemStack output=player.containerMenu.getSlot(0).getItem();
            if(output.isEmpty()){if(now>j.stageDeadline)throw new IllegalStateException("材料不足或服务器拒绝配方");return;}
            if(!BuiltInRegistries.ITEM.getKey(output.getItem()).toString().equals(j.item))throw new IllegalStateException("unexpected recipe output");
            click(player,0,0,ClickType.QUICK_MOVE);j.next=now+700;j.stage=1;
        } else {
            int gained=count(player,j.item)-j.baseline;j.detail.addProperty("produced",gained);
            if(gained>=j.count){finish(true,null);return;}
            if(gained<=j.completed)throw new IllegalStateException("craft output did not enter inventory");
            j.completed=gained;placeCraftRecipe(j,player);j.stage=0;j.next=now+250;j.stageDeadline=now+2500;
        }
    }

    private void beginWorkbench(Job j,LocalPlayer player,JsonObject p) {
        String mode=text(p,"operation","open");j.pos=position(p);j.detail.add("position",xyz(j.pos));
        if(mode.equals("open")) {
            if(!player.level().getBlockState(j.pos).is(Blocks.CRAFTING_TABLE))throw new IllegalStateException("目标不是工作台");
            if(player.containerMenu!=player.inventoryMenu)throw new IllegalStateException("先关闭已有容器");
            useBlock(player,j.pos);
        } else if(mode.equals("place")) {
            normalInventory(player);
            if(!placementSite(player,j.pos))throw new IllegalStateException("放置位置被占用、没有稳定地面或被视线遮挡");
            int source=find(player,"minecraft:crafting_table");if(source<0)throw new IllegalStateException("背包没有工作台");
            select(player,hotbar(player,source));useTop(player,j.pos.below());j.stage=1;
        } else throw new IllegalArgumentException("workbench mode must be open or place");
        j.next=j.started+700;j.deadline=j.started+7000;
    }
    private void tickWorkbench(Job j,LocalPlayer player) {
        boolean done=j.stage==0?player.containerMenu instanceof CraftingMenu:player.level().getBlockState(j.pos).is(Blocks.CRAFTING_TABLE);
        if(done){j.detail.addProperty("open",j.stage==0);j.detail.addProperty("placed",j.stage==1);finish(true,null);}
    }
    private void beginStation(Job j,LocalPlayer player,JsonObject p) {
        normalInventory(player);j.pos=position(p);j.item=SurvivalRules.itemId(text(p,"item",""));
        if(!Set.of("minecraft:furnace","farmersdelight:cutting_board","minecraft:torch").contains(j.item))throw new IllegalArgumentException("unsupported station; arbitrary block placement is disabled");
        if(text(p,"operation","place").equals("recover")) {
            if(!j.item.equals("minecraft:furnace"))throw new IllegalArgumentException("only owned empty furnaces support recovery");
            if(!BuiltInRegistries.BLOCK.getKey(player.level().getBlockState(j.pos).getBlock()).toString().equals(j.item))throw new IllegalStateException("owned station changed; recovery refused");
            // Client block-entity inventory is not authoritative. Read synchronized menu slots first.
            useBlock(player,j.pos);j.kind="recover_station";j.stage=0;j.next=j.started+700;return;
        }
        if(!placementSite(player,j.pos))throw new IllegalStateException("station placement is occupied, unstable or obscured");
        int source=find(player,j.item);if(source<0)throw new IllegalStateException("station is not in inventory");
        select(player,hotbar(player,source));useTop(player,j.pos.below());j.next=j.started+700;j.deadline=j.started+7000;
        j.detail.addProperty("item",j.item);j.detail.add("position",xyz(j.pos));
    }
    private void beginInteract(Job j,LocalPlayer player,JsonObject p) {
        normalInventory(player);j.pos=position(p);String mode=text(p,"operation","open");
        if(!Set.of("open","close").contains(mode))throw new IllegalArgumentException("only explicit door open/close is supported");
        BlockState state=player.level().getBlockState(j.pos);
        if(!(state.getBlock() instanceof DoorBlock door)||!door.type().canOpenByHand())throw new IllegalStateException("target is not a hand-openable door; buttons/levers are not operated");
        j.count=mode.equals("open")?1:0;
        if(state.getValue(DoorBlock.OPEN)!=(j.count==1))useBlock(player,j.pos);
        j.detail.add("position",xyz(j.pos));j.next=j.started+500;j.deadline=j.started+4000;
    }
    void beforeTick() {
        if(job==null||!Set.of("pickup","align").contains(job.kind)||job.origin!=connection.get()||job.generation!=generation.getAsLong())return;
        Minecraft mc=Minecraft.getInstance();mc.options.keyUp.setDown(true);mc.options.keyShift.setDown(true);
    }
    private void beginMineBelow(Job j,LocalPlayer player) {
        JsonObject p=new JsonObject();p.addProperty("depth",1);beginDigDown(j,player,p);
    }

    private JsonObject interactBlock(LocalPlayer player,JsonObject p) {
        BlockPos pos=position(p);Level level=player.level();BlockState state=level.getBlockState(pos);
        if(!(state.getBlock() instanceof DoorBlock||state.getBlock() instanceof FenceGateBlock||
             state.getBlock() instanceof ButtonBlock||state.getBlock() instanceof LeverBlock||
             state.getBlock() instanceof BedBlock||state.getBlock() instanceof TrapDoorBlock))
            throw new IllegalStateException("block is not in the supported interaction whitelist");
        if(state.getBlock() instanceof BedBlock&&state.getValue(BedBlock.OCCUPIED))
            throw new IllegalStateException("bed is occupied");
        visible(player,pos);useBlock(player,pos);
        JsonObject result=new JsonObject();result.addProperty("block",blockId(state));result.addProperty("interacted",true);return result;
    }

    private void beginDigDown(Job j,LocalPlayer player,JsonObject p) {
        int depth=Math.max(1,Math.min(8,integer(p,"depth",3)));
        j.count=depth;
        j.detail.addProperty("requested_depth",depth);
        j.deadline=j.started+Math.max(26000,depth*7000);
        digDownLayer(j,player);
    }

    private void digDownLayer(Job j,LocalPlayer player) {
        if(!player.onGround()||player.isInWater()||player.isInLava())throw new IllegalStateException("player is not standing safely for downward digging");
        if(Minecraft.getInstance().screen!=null)throw new IllegalStateException("close the current screen before digging");
        BlockPos target=player.blockPosition().below();Level level=player.level();
        if(!level.hasChunkAt(target)||level.isOutsideBuildHeight(target))throw new IllegalStateException("target is outside the loaded world");
        BlockState state=level.getBlockState(target);
        if(state.isAir()||!state.getFluidState().isEmpty())throw new IllegalStateException("block below feet is air or fluid");
        if(state.getDestroySpeed(level,target)<0)throw new IllegalStateException("block below feet is unbreakable");
        if(state.getBlock() instanceof FallingBlock)throw new IllegalStateException("refusing to dig under a falling block");
        if(level.getBlockEntity(target)!=null)throw new IllegalStateException("refusing to dig a block entity");
        BlockPos support=target.below();BlockState under=level.getBlockState(support);
        if(under.isAir()||!under.getFluidState().isEmpty()||under.getDestroySpeed(level,support)<0)
            throw new IllegalStateException("no safe floor below the next shaft block");
        if(player.getEyePosition().distanceTo(Vec3.atCenterOf(target))>4.5)throw new IllegalStateException("block below feet is outside interaction reach");
        j.pos=target;j.oldBlock=state;j.face=Direction.UP;j.stage=0;j.progress=0;j.next=j.started+350;
        int best=player.getInventory().selected;float speed=-1;
        for(int i=0;i<36;i++) {
            ItemStack stack=player.getInventory().getItem(i);
            if(state.requiresCorrectToolForDrops()&&!stack.isCorrectToolForDrops(state))continue;
            float candidate=stack.getDestroySpeed(state);if(candidate>speed){speed=candidate;best=i;}
        }
        if(speed<0)throw new IllegalStateException("no suitable tool for the block below feet");
        select(player,hotbar(player,best));
        j.detail.addProperty("current_block",blockId(state));j.detail.add("current_position",xyz(target));
    }

    private void tickDigDown(Job j,LocalPlayer player,long now) {
        BlockState current=player.level().getBlockState(j.pos);
        if(!current.equals(j.oldBlock)) {
            if(!current.isAir()&&!current.getFluidState().isEmpty())throw new IllegalStateException("shaft block changed to a fluid or solid block");
            if(j.stage!=2){Minecraft.getInstance().options.keyAttack.setDown(false);Minecraft.getInstance().gameMode.stopDestroyBlock();j.stage=2;j.next=now+300;return;}
            if(player.onGround()&&player.blockPosition().getY()<=j.pos.getY()) {
                j.count--;
                if(j.count<=0){j.detail.addProperty("depth",j.detail.get("requested_depth").getAsInt());finish(true,null);return;}
                digDownLayer(j,player);j.next=now+300;return;
            }
            if(now>j.deadline)throw new IllegalStateException("player did not descend into the shaft");
            j.next=now+150;return;
        }
        if(j.stage==3){if(now>j.stageDeadline)throw new IllegalStateException("server did not confirm the shaft block break; area may be protected");j.next=now+100;return;}
        visible(player,j.pos);player.lookAt(EntityAnchorArgument.Anchor.EYES,Vec3.atCenterOf(j.pos));
        if(j.stage==0){player.connection.send(new ServerboundPlayerActionPacket(ServerboundPlayerActionPacket.Action.START_DESTROY_BLOCK,j.pos,j.face));j.stage=1;}
        if(j.stage==1) {
            j.progress+=j.oldBlock.getDestroyProgress(player,player.level(),j.pos);
            player.level().destroyBlockProgress(player.getId(),j.pos,Math.min(9,(int)(j.progress*10)-1));
            player.swing(InteractionHand.MAIN_HAND);
            if(j.progress>=1){player.connection.send(new ServerboundPlayerActionPacket(ServerboundPlayerActionPacket.Action.STOP_DESTROY_BLOCK,j.pos,j.face));j.stage=3;j.stageDeadline=now+3000;}
        }
    }

    private void beginPlant(Job j,LocalPlayer player,JsonObject p) {
        j.pos=position(p);Level level=player.level();
        if(!level.getBlockState(j.pos).is(Blocks.FARMLAND))throw new IllegalStateException("plant target must be farmland");
        BlockPos above=j.pos.above();
        if(!level.getBlockState(above).canBeReplaced())throw new IllegalStateException("plant space is occupied");
        String item=SurvivalRules.itemId(text(p,"item","minecraft:wheat_seeds"));
        int source=find(player,item);if(source<0)throw new IllegalStateException("seed item is not in inventory: "+item);
        select(player,hotbar(player,source));
        Vec3 point=new Vec3(j.pos.getX()+.5,j.pos.getY()+1.0,j.pos.getZ()+.5);
        player.lookAt(EntityAnchorArgument.Anchor.EYES,point);
        BlockHitResult hit=new BlockHitResult(point,Direction.UP,j.pos,false);
        Minecraft.getInstance().gameMode.useItemOn(player,InteractionHand.MAIN_HAND,hit);player.swing(InteractionHand.MAIN_HAND);
        j.detail.add("position",xyz(j.pos));j.detail.addProperty("item",item);j.stage=0;j.next=j.started+500;j.deadline=j.started+4000;
    }

    private void tickPlant(Job j,LocalPlayer player,long now) {
        BlockState above=player.level().getBlockState(j.pos.above());
        if(above.getBlock() instanceof CropBlock){j.detail.addProperty("planted",true);finish(true,null);return;}
        if(now>j.deadline)throw new IllegalStateException("planting was not confirmed");
        j.next=now+200;
    }

    private void beginTill(Job j,LocalPlayer player,JsonObject p) {
        normalInventory(player);j.pos=position(p);Level level=player.level();
        BlockState state=level.getBlockState(j.pos);
        if(!(state.is(Blocks.DIRT)||state.is(Blocks.GRASS_BLOCK)||state.is(Blocks.DIRT_PATH)))
            throw new IllegalStateException("till target must be dirt, grass or dirt path");
        if(!level.getBlockState(j.pos.above()).canBeReplaced())
            throw new IllegalStateException("till target must have replaceable space above");
        String requestedText=text(p,"item","").trim();
        String requested=requestedText.isEmpty()?"":SurvivalRules.itemId(requestedText);int source=-1;
        for(int i=0;i<36;i++) {
            ItemStack stack=player.getInventory().getItem(i);
            if(!(stack.getItem() instanceof HoeItem))continue;
            String key=BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
            if(!requested.isEmpty()&&!key.equals(requested))continue;
            source=i;break;
        }
        if(source<0)throw new IllegalStateException("NO_TOOL: no hoe in inventory"+(requested.isEmpty()?"":" for "+requested));
        select(player,hotbar(player,source));useTop(player,j.pos);
        String used=BuiltInRegistries.ITEM.getKey(player.getMainHandItem().getItem()).toString();
        j.detail.add("position",xyz(j.pos));j.detail.addProperty("item",used);j.next=j.started+400;j.deadline=j.started+3000;
    }

    private void tickTill(Job j,LocalPlayer player,long now) {
        BlockState state=player.level().getBlockState(j.pos);
        if(state.is(Blocks.FARMLAND)){j.detail.addProperty("tilled",true);finish(true,null);return;}
        if(!(state.is(Blocks.DIRT)||state.is(Blocks.GRASS_BLOCK)||state.is(Blocks.DIRT_PATH)))
            throw new IllegalStateException("till target changed to an unsupported block");
        if(now>j.deadline)throw new IllegalStateException("tilling was not confirmed");
        j.next=now+150;
    }

    private void beginBonemeal(Job j,LocalPlayer player,JsonObject p) {
        normalInventory(player);j.pos=position(p);Level level=player.level();
        BlockState state=level.getBlockState(j.pos);
        if(!(state.getBlock() instanceof CropBlock crop))
            throw new IllegalStateException("use_bonemeal only supports crops");
        if(state.getValue(CropBlock.AGE)>=crop.getMaxAge())
            throw new IllegalStateException("crop is already fully grown");
        String requestedText=text(p,"item","").trim();
        String item=requestedText.isEmpty()?"minecraft:bone_meal":SurvivalRules.itemId(requestedText);
        int source=find(player,item);if(source<0)throw new IllegalStateException("NO_TOOL: item is not in inventory: "+item);
        select(player,hotbar(player,source));useTop(player,j.pos);
        j.oldBlock=state;j.item=item;
        j.detail.add("position",xyz(j.pos));j.detail.addProperty("item",item);
        j.detail.addProperty("block_before",blockId(state));j.detail.addProperty("age_before",state.getValue(CropBlock.AGE));
        j.next=j.started+300;j.deadline=j.started+2500;
    }

    private void tickBonemeal(Job j,LocalPlayer player,long now) {
        BlockState state=player.level().getBlockState(j.pos);
        if(!state.toString().equals(j.oldBlock.toString())) {
            j.detail.addProperty("block_after",blockId(state));
            if(state.getBlock() instanceof CropBlock)j.detail.addProperty("age_after",state.getValue(CropBlock.AGE));
            j.detail.addProperty("bonemealed",true);finish(true,null);return;
        }
        if(now>j.deadline)throw new IllegalStateException("bonemeal was not confirmed");
        j.next=now+150;
    }

    private void beginJump(Job j,LocalPlayer player) {
        if(!player.onGround()||player.isInWater()||player.isInLava())
            throw new IllegalStateException("jump requires standing on ground and out of water/lava");
        j.originPosition=player.position();player.input.jumping=true;
        Minecraft.getInstance().options.keyJump.setDown(true);
        j.detail.addProperty("start_y",j.originPosition.y);j.next=j.started+50;j.deadline=j.started+1200;
    }

    private void tickJump(Job j,LocalPlayer player,long now) {
        if(!player.onGround()||player.getY()>j.originPosition.y+0.05) {
            player.input.jumping=false;Minecraft.getInstance().options.keyJump.setDown(false);
            j.detail.addProperty("jumped",true);finish(true,null);return;
        }
        if(now>j.deadline){player.input.jumping=false;Minecraft.getInstance().options.keyJump.setDown(false);throw new IllegalStateException("jump was not confirmed");}
        j.next=now+50;
    }

    private void beginPickup(Job j,LocalPlayer player,JsonObject p) {
        normalInventory(player);j.item=SurvivalRules.itemId(text(p,"item",""));
        java.util.UUID wanted=java.util.UUID.fromString(text(p,"entity_uuid",""));
        j.drop=player.level().getEntitiesOfClass(ItemEntity.class,player.getBoundingBox().inflate(2.5)).stream()
            .filter(e->e.isAlive()&&e.getUUID().equals(wanted)&&BuiltInRegistries.ITEM.getKey(e.getItem().getItem()).toString().equals(j.item)).findFirst().orElse(null);
        if(j.drop==null||!player.onGround()||j.drop.distanceTo(player)>2.4||Math.abs(j.drop.getY()-player.getY())>1.25||!player.hasLineOfSight(j.drop))throw new IllegalStateException("PICKUP_UNAVAILABLE: drop is not safely reachable nearby");
        Vec3 direction=j.drop.position().subtract(player.position());double horizontal=Math.hypot(direction.x,direction.z);
        if(horizontal<.05)throw new IllegalStateException("PICKUP_UNAVAILABLE: no safe horizontal approach");
        BlockPos ahead=BlockPos.containing(player.getX()+direction.x/horizontal*.5,Math.floor(player.getY()+.125),player.getZ()+direction.z/horizontal*.5);
        if(!standable(player.level(),ahead))throw new IllegalStateException("PICKUP_UNAVAILABLE: forward step has no safe floor/headroom");
        player.connection.sendChat("#stop");player.lookAt(EntityAnchorArgument.Anchor.EYES,j.drop.position());
        j.originPosition=player.position();j.baseline=count(player,j.item);j.next=j.started+100;j.deadline=j.started+2200;
        j.detail.addProperty("item",j.item);
    }
    private void beginAlign(Job j,LocalPlayer player,JsonObject p){
        normalInventory(player);j.pos=position(p);double feet=TerrainQueries.height(player,j.pos,Set.of());
        Vec3 target=new Vec3(j.pos.getX()+.5,feet,j.pos.getZ()+.5);
        if(!Double.isFinite(feet)||!player.onGround()||Math.hypot(player.getX()-target.x,player.getZ()-target.z)>1.2||Math.abs(player.getY()-feet)>.65)throw new IllegalStateException("alignment requires a nearby verified standing cell");
        Vec3 from=player.position();for(int i=1;i<=8;i++){Vec3 step=from.lerp(target,i/8.0);BlockPos cell=new BlockPos((int)Math.floor(step.x),TerrainQueries.feetBlockY(step.y),(int)Math.floor(step.z));if(!localStandable(player,cell,Set.of()))throw new IllegalStateException("alignment route is not safe");}
        j.originPosition=from;j.deadline=j.started+1800;j.next=j.started;player.lookAt(EntityAnchorArgument.Anchor.EYES,new Vec3(target.x,player.getEyeY(),target.z));
    }
    private void tickAlign(Job j,LocalPlayer player,long now){
        if(Math.hypot(player.getX()-j.pos.getX()-.5,player.getZ()-j.pos.getZ()-.5)<=.045){j.detail.addProperty("aligned",true);finish(true,null);return;}
        if(player.getHealth()<=8||player.isOnFire()||player.position().distanceTo(j.originPosition)>1.2)throw new IllegalStateException("alignment interrupted by danger or movement budget");
        player.lookAt(EntityAnchorArgument.Anchor.EYES,new Vec3(j.pos.getX()+.5,player.getEyeY(),j.pos.getZ()+.5));j.next=now+50;
    }
    private void beginLadderAssist(Job j,LocalPlayer player,JsonObject p){
        if(player.isDeadOrDying()||player.isSleeping()||player.isCreative()||player.isSpectator())throw new IllegalStateException("survival player unavailable");
        if(player.isInWater()||player.isInLava()||player.isPassenger())throw new IllegalStateException("ladder assist requires dry ground");
        int targetY=integer(p,"y",player.blockPosition().getY());
        if(targetY<=player.getY()+.25)throw new IllegalStateException("ladder assist is only for upward entry");
        BlockPos feet=player.blockPosition();
        BlockPos best=null;
        if(player.level().getBlockState(feet).is(Blocks.LADDER)){best=feet;j.detail.addProperty("already_on_ladder",true);}
        else{double bestScore=Double.MAX_VALUE;
            for(int dx=-2;dx<=2;dx++)for(int dy=-1;dy<=2;dy++)for(int dz=-2;dz<=2;dz++){
                BlockPos pos=feet.offset(dx,dy,dz);if(!player.level().getBlockState(pos).is(Blocks.LADDER))continue;
                double horizontal=Math.hypot(pos.getX()+.5-player.getX(),pos.getZ()+.5-player.getZ());if(horizontal>3.5)continue;
                double score=horizontal+Math.abs(pos.getY()-feet.getY())*.15;
                if(score<bestScore){bestScore=score;best=pos.immutable();}
            }
        }
        if(best==null)throw new IllegalStateException("no ladder within 3.5 blocks");
        j.pos=best;j.originPosition=player.position();j.stage=0;j.next=j.started;j.deadline=j.started+4000;
        j.detail.addProperty("ladder",best.getX()+","+best.getY()+","+best.getZ());j.detail.addProperty("start_y",player.getY());
        player.connection.sendChat("#stop");pathFeedback.end();navigationTarget=null;
        Minecraft.getInstance().options.keyUp.setDown(false);Minecraft.getInstance().options.keyJump.setDown(false);player.input.jumping=false;
        aimAtLadder(player,best);
    }
    private void aimAtLadder(LocalPlayer player,BlockPos ladder){
        BlockState state=player.level().getBlockState(ladder);
        Direction facing=state.is(Blocks.LADDER)?state.getValue(LadderBlock.FACING):Direction.NORTH;
        double dx=ladder.getX()+.5-player.getX(),dz=ladder.getZ()+.5-player.getZ();
        if(Math.hypot(dx,dz)<.3){float yaw=facing.getOpposite().toYRot();player.setYRot(yaw);player.setYHeadRot(yaw);player.setXRot(0f);}
        else player.lookAt(EntityAnchorArgument.Anchor.EYES,new Vec3(ladder.getX()+.5,player.getEyeY(),ladder.getZ()+.5));
    }
    private void tickLadderAssist(Job j,LocalPlayer player,long now){
        if(j.detail.has("entered")&&j.detail.get("entered").getAsBoolean()){j.detail.addProperty("end_y",player.getY());finish(true,null);return;}
        if(player.getHealth()<=8||player.isOnFire()||player.isInWater()||player.isInLava()||player.isPassenger())throw new IllegalStateException("ladder assist interrupted by danger");
        if(player.position().distanceTo(j.originPosition)>4.0)throw new IllegalStateException("ladder assist moved too far");
        BlockPos feet=player.blockPosition();boolean onLadder=player.level().getBlockState(feet).is(Blocks.LADDER)||player.onClimbable();
        if(onLadder&&player.getY()>j.originPosition.y+.12){
            j.stage++;
            if(j.stage>=4||player.getY()>=j.pos.getY()+.9){j.detail.addProperty("entered",true);j.detail.addProperty("end_y",player.getY());finish(true,null);return;}
        }
        if(now>j.deadline)throw new IllegalStateException("ladder entry was not confirmed");
        if(now>=j.next){aimAtLadder(player,j.pos);Minecraft.getInstance().options.keyUp.setDown(true);Minecraft.getInstance().options.keyJump.setDown(true);player.input.jumping=true;j.next=now+100;}
    }
    private void tickPickup(Job j,LocalPlayer player,long now) {
        int gained=count(player,j.item)-j.baseline;
        if(gained>0){j.detail.addProperty("produced",gained);j.detail.addProperty("collected",true);finish(true,null);return;}
        if(!j.drop.isAlive()||player.position().distanceTo(j.originPosition)>.6||now-j.started>1200)throw new IllegalStateException("PICKUP_NO_GAIN: no inventory increase after bounded normal movement");
        if(player.getHealth()<=8||player.isOnFire())throw new IllegalStateException("unsafe player state during pickup");
        j.next=now+50;
    }
    private void beginSmelt(Job j,LocalPlayer player,JsonObject p) {
        normalInventory(player);j.pos=position(p);j.item=SurvivalRules.itemId(text(p,"item",""));
        j.count=SurvivalRules.checkedCount(integer(p,"count",1));if(j.count>8)throw new IllegalArgumentException("smelting is limited to 8 outputs per batch");
        j.collectOnly=p.has("collect_only")&&p.get("collect_only").getAsBoolean();
        if(!player.level().getBlockState(j.pos).is(Blocks.FURNACE))throw new IllegalStateException("target is not a normal furnace");
        if(player.getInventory().getFreeSlot()<0)throw new IllegalStateException("inventory is full; no smelt started");
        j.baseline=count(player,j.item);j.detail.addProperty("item",j.item);j.detail.add("position",xyz(j.pos));
        useBlock(player,j.pos);j.next=j.started+800;j.deadline=j.started+7000;
    }
    private void insertExact(LocalPlayer player,int source,int destination,int amount) {
        Slot from=slot(player,source),to=player.containerMenu.getSlot(destination);
        if(amount<1||amount>16||from.getItem().getCount()<amount||!to.getItem().isEmpty()||!to.mayPlace(from.getItem()))throw new IllegalStateException("exact furnace transfer is unavailable; inventory left untouched");
        click(player,from.index,0,ClickType.PICKUP);
        for(int i=0;i<amount;i++)click(player,to.index,1,ClickType.PICKUP);
        if(!player.containerMenu.getCarried().isEmpty())click(player,from.index,0,ClickType.PICKUP);
        if(!player.containerMenu.getCarried().isEmpty())throw new IllegalStateException("transfer cursor unexpectedly nonempty; stop and inspect");
    }
    private void tickSmelt(Job j,LocalPlayer player,long now) {
        if(!(player.containerMenu instanceof FurnaceMenu menu)) {
            if(j.stage>0||now>j.started+5000)throw new IllegalStateException("furnace menu unavailable or changed; inserted materials stay in your furnace");
            j.next=now+200;return;
        }
        if(j.stage==0) {
            j.menu=menu.containerId;
            if(j.collectOnly) {j.stage=2;j.deadline=now+5000;}
            else {
                for(int i=0;i<3;i++)if(!menu.getSlot(i).getItem().isEmpty())throw new IllegalStateException("furnace contains materials/output; inspect or collect first, nothing replaced");
                int input=-1;AbstractCookingRecipe recipe=null;
                for(var holder:player.level().getRecipeManager().getAllRecipesFor(RecipeType.SMELTING)) {
                    var candidate=holder.value();
                    if(!BuiltInRegistries.ITEM.getKey(candidate.getResultItem(player.registryAccess()).getItem()).toString().equals(j.item))continue;
                    for(int i=0;i<36;i++)if(candidate.getIngredients().getFirst().test(player.getInventory().getItem(i))) {
                        int needed=(j.count+candidate.getResultItem(player.registryAccess()).getCount()-1)/candidate.getResultItem(player.registryAccess()).getCount();
                        if(player.getInventory().getItem(i).getCount()>=needed){input=i;recipe=candidate;break;}
                    }
                    if(recipe!=null)break;
                }
                if(recipe==null)throw new IllegalStateException("no matching smelting recipe and sufficient input in inventory");
                int needed=(j.count+recipe.getResultItem(player.registryAccess()).getCount()-1)/recipe.getResultItem(player.registryAccess()).getCount();
                int fuel=-1,fuelCount=0;
                for(String key:new String[]{"minecraft:coal","minecraft:charcoal"}) {
                    int index=find(player,key);
                    if(index<0)continue;
                    int burn=player.getInventory().getItem(index).getBurnTime(RecipeType.SMELTING);
                    if(burn<=0)continue;
                    int n=(recipe.getCookingTime()*needed+burn-1)/burn;
                    if(!key.equals(j.item)&&player.getInventory().getItem(index).getCount()>=n){fuel=index;fuelCount=n;break;}
                }
                if(fuel<0)for(int i=0;i<36;i++) {
                    ItemStack stack=player.getInventory().getItem(i);int burn=stack.getBurnTime(RecipeType.SMELTING);
                    if(burn<=0)continue;
                    int n=(recipe.getCookingTime()*needed+burn-1)/burn;
                    if(BuiltInRegistries.ITEM.getKey(stack.getItem()).toString().endsWith("_planks")&&stack.getCount()>=n){fuel=i;fuelCount=n;break;}
                }
                if(fuel<0||fuelCount>16)throw new IllegalStateException("not enough approved fuel (coal/charcoal/planks); tools and lava are never used");
                j.detail.addProperty("input",BuiltInRegistries.ITEM.getKey(player.getInventory().getItem(input).getItem()).toString());
                j.detail.addProperty("input_count",needed);j.detail.addProperty("fuel",BuiltInRegistries.ITEM.getKey(player.getInventory().getItem(fuel).getItem()).toString());j.detail.addProperty("fuel_count",fuelCount);
                insertExact(player,input,0,needed);insertExact(player,fuel,1,fuelCount);
                j.stage=1;j.deadline=now+Math.min(150000,recipe.getCookingTime()*needed*50L+30000);j.next=now+700;return;
            }
        }
        if(menu.containerId!=j.menu)throw new IllegalStateException("furnace menu changed; no completion confirmed");
        if(j.stage==1||j.stage==2) {
            ItemStack output=menu.getSlot(2).getItem();
            if(!output.isEmpty()&&!BuiltInRegistries.ITEM.getKey(output.getItem()).toString().equals(j.item))throw new IllegalStateException("unexpected furnace output; not taken");
            if(output.isEmpty()&&j.collectOnly)throw new IllegalStateException("requested output is not ready; input and fuel left untouched");
            if(!output.isEmpty()&&(j.collectOnly||output.getCount()>=j.count)) {
                click(player,2,0,ClickType.QUICK_MOVE);j.stage=3;j.next=now+700;return;
            }
            j.next=now+400;
        } else if(j.stage==3) {
            int gain=count(player,j.item)-j.baseline;
            if(gain<(j.collectOnly?1:j.count))throw new IllegalStateException("furnace output not confirmed in inventory; inspect your furnace");
            j.detail.addProperty("produced",gain);j.detail.addProperty("collected",true);finish(true,null);
        }
    }
    private void beginSleep(Job j,LocalPlayer player,JsonObject p) {
        normalInventory(player);
        if(player.level().dimension()!=Level.OVERWORLD||!player.level().dimensionType().bedWorks())throw new IllegalStateException("只允许在主世界睡觉，禁止在会爆炸的维度使用床");
        if(!SurvivalRules.night(player.level().getDayTime())&&!player.level().isThundering())throw new IllegalStateException("现在不是可睡觉的时间");
        j.pos=position(p);BlockState bed=player.level().getBlockState(j.pos);
        if(!(bed.getBlock() instanceof BedBlock)||bed.getValue(BedBlock.OCCUPIED))throw new IllegalStateException("目标没有空床");
        useBlock(player,j.pos);j.next=j.started+500;j.deadline=j.started+7000;
    }
    private void useBlock(LocalPlayer player,BlockPos pos) {
        if(player.isShiftKeyDown())throw new IllegalStateException("player is sneaking; interaction refused");
        BlockHitResult hit=visible(player,pos);
        player.lookAt(EntityAnchorArgument.Anchor.EYES,hit.getLocation());
        Minecraft.getInstance().gameMode.useItemOn(player,InteractionHand.MAIN_HAND,hit);player.swing(InteractionHand.MAIN_HAND);
    }
    private BlockHitResult visible(LocalPlayer player,BlockPos pos) {
        try{return visibleAt(player,pos,Vec3.atCenterOf(pos));}catch(IllegalStateException blocked){}
        for(Direction face:Direction.values()) {
            Vec3 point=Vec3.atCenterOf(pos).add(face.getStepX()*.499,face.getStepY()*.499,face.getStepZ()*.499);
            try{return visibleAt(player,pos,point);}catch(IllegalStateException blocked){}
        }
        throw new IllegalStateException("block is not visible or out of reach; refusing to interact through walls");
    }
    private void useTop(LocalPlayer player,BlockPos pos) {
        double top=player.level().getBlockState(pos).getCollisionShape(player.level(),pos).max(Direction.Axis.Y);
        BlockHitResult hit=visibleAt(player,pos,new Vec3(pos.getX()+0.5,pos.getY()+top-0.01,pos.getZ()+0.5));
        player.lookAt(EntityAnchorArgument.Anchor.EYES,hit.getLocation());
        Minecraft.getInstance().gameMode.useItemOn(player,InteractionHand.MAIN_HAND,hit);player.swing(InteractionHand.MAIN_HAND);
    }
    private BlockHitResult visibleAt(LocalPlayer player,BlockPos pos,Vec3 destination) {
        if(player.getEyePosition().distanceTo(destination)>4.5)throw new IllegalStateException("block is outside interaction reach");
        BlockHitResult hit=player.level().clip(new ClipContext(player.getEyePosition(),destination,ClipContext.Block.OUTLINE,ClipContext.Fluid.NONE,player));
        if(hit.getType()!=HitResult.Type.BLOCK||!hit.getBlockPos().equals(pos))throw new IllegalStateException("block is not visible; refusing to interact through walls");
        return hit;
    }

    private void beginMine(Job j,LocalPlayer player,JsonObject p) {
        normalInventory(player);j.pos=position(p);String resource=text(p,"resource","");
        if(j.recoverStation&&!BuiltInRegistries.BLOCK.getKey(player.level().getBlockState(j.pos).getBlock()).toString().equals(j.item))throw new IllegalStateException("owned station changed before recovery; refusing to mine replacement block");
        if(Minecraft.getInstance().screen!=null)throw new IllegalStateException("close the current screen before mining");
        if(!j.recoverStation&&!natural(player,j.pos,resource))throw new IllegalStateException("目标不符合自然资源保护规则");
        if(!SurvivalRules.safeMiningCell(j.pos.getX(),j.pos.getY(),j.pos.getZ(),player.getX(),player.getY(),player.getZ()))throw new IllegalStateException("禁止挖承重脚下或低于脚下两格的方块");
        j.face=visible(player,j.pos).getDirection();j.oldBlock=player.level().getBlockState(j.pos);
        int best=player.getInventory().selected;float speed=-1;
        for(int i=0;i<36;i++) {
            ItemStack stack=player.getInventory().getItem(i);
            if(j.oldBlock.requiresCorrectToolForDrops()&&!stack.isCorrectToolForDrops(j.oldBlock))continue;
            float candidate=stack.getDestroySpeed(j.oldBlock);if(candidate>speed){speed=candidate;best=i;}
        }
        if(speed<0)throw new IllegalStateException("没有能获取掉落物的合适工具");
        select(player,hotbar(player,best));j.stage=0;j.next=j.started+700;
        j.detail.add("position",xyz(j.pos));j.detail.addProperty("resource",resource);
    }
    private void tickMine(Job j,LocalPlayer player,long now) {
        BlockState current=player.level().getBlockState(j.pos);
        if(!current.equals(j.oldBlock)) {
            if(!current.isAir()&&!j.kind.equals("mine_at"))throw new IllegalStateException("target block changed unexpectedly");
            if(j.stage!=2){Minecraft.getInstance().options.keyAttack.setDown(false);Minecraft.getInstance().gameMode.stopDestroyBlock();j.stage=2;j.next=now+700;return;}
            j.detail.addProperty("block_broken",true);j.detail.addProperty("items_collected",false);
            // Seed replanting is attempted only on the farmland just harvested.
            if(j.oldBlock.getBlock() instanceof CropBlock && player.level().getBlockState(j.pos.below()).is(Blocks.FARMLAND)) {
                int seed=find(player,"minecraft:wheat_seeds");
                if(seed>=0){select(player,hotbar(player,seed));useTop(player,j.pos.below());j.detail.addProperty("replant_attempted",true);}
            }
            finish(true,null);return;
        }
        visible(player,j.pos);player.lookAt(EntityAnchorArgument.Anchor.EYES,Vec3.atCenterOf(j.pos));
        if(j.stage==0){player.connection.send(new ServerboundPlayerActionPacket(ServerboundPlayerActionPacket.Action.START_DESTROY_BLOCK,j.pos,j.face));j.stage=1;}
        if(j.stage==1) {
            j.progress+=j.oldBlock.getDestroyProgress(player,player.level(),j.pos);
            player.level().destroyBlockProgress(player.getId(),j.pos,Math.min(9,(int)(j.progress*10)-1));
            player.swing(InteractionHand.MAIN_HAND);
            if(j.progress>=1){player.connection.send(new ServerboundPlayerActionPacket(ServerboundPlayerActionPacket.Action.STOP_DESTROY_BLOCK,j.pos,j.face));j.stage=3;}
        }
    }

    private void beginMineAt(Job j,LocalPlayer player,JsonObject p) {
        normalInventory(player);j.pos=position(p);
        if(Minecraft.getInstance().screen!=null)throw new IllegalStateException("close the current screen before mining");
        Level level=player.level();
        if(!level.hasChunkAt(j.pos))throw new IllegalStateException("target chunk is not loaded");
        BlockState state=level.getBlockState(j.pos);
        if(state.isAir()||!state.getFluidState().isEmpty())throw new IllegalStateException("target is air or fluid");
        if(state.getDestroySpeed(level,j.pos)<0)throw new IllegalStateException("target block is unbreakable");
        if(player.getEyePosition().distanceTo(Vec3.atCenterOf(j.pos))>4.5)throw new IllegalStateException("target block is outside interaction reach");
        boolean safe=SurvivalRules.safeMiningCell(j.pos.getX(),j.pos.getY(),j.pos.getZ(),player.getX(),player.getY(),player.getZ());
        if(!safe && player.onGround() && j.pos.equals(player.blockPosition().below())
                && SurvivalRules.safeUnderfootMiningCell(j.pos.getX(),j.pos.getY(),j.pos.getZ(),player.getX(),player.getY(),player.getZ())) {
            BlockPos support=j.pos.below();
            BlockState under=level.getBlockState(support);
            safe=!under.isAir()&&under.getFluidState().isEmpty()&&under.getDestroySpeed(level,support)>=0
                    &&! (under.getBlock() instanceof FallingBlock)&&level.getBlockEntity(support)==null;
        }
        if(!safe)throw new IllegalStateException("refusing to mine a load-bearing or below-feet block");
        j.face=visible(player,j.pos).getDirection();j.oldBlock=state;
        int best=player.getInventory().selected;float speed=-1;
        for(int i=0;i<36;i++) {
            ItemStack stack=player.getInventory().getItem(i);
            if(state.requiresCorrectToolForDrops()&&!stack.isCorrectToolForDrops(state))continue;
            float candidate=stack.getDestroySpeed(state);if(candidate>speed){speed=candidate;best=i;}
        }
        if(speed<0)throw new IllegalStateException("no suitable tool for this block");
        select(player,hotbar(player,best));j.stage=0;j.next=j.started+700;
        j.detail.add("position",xyz(j.pos));j.detail.addProperty("block",blockId(state));
    }

    private BlockHitResult placementHit(LocalPlayer player,BlockPos support,Direction face,Vec3 point) {
        if(player.getEyePosition().distanceTo(point)>4.5)throw new IllegalStateException("placement face is outside interaction reach");
        BlockHitResult clip=player.level().clip(new ClipContext(player.getEyePosition(),point,ClipContext.Block.COLLIDER,ClipContext.Fluid.NONE,player));
        if(clip.getType()==HitResult.Type.BLOCK&&!clip.getBlockPos().equals(support))throw new IllegalStateException("placement face is blocked by another full block");
        return new BlockHitResult(point,face,support,false);
    }

    private void beginPlaceAt(Job j,LocalPlayer player,JsonObject p) {
        normalInventory(player);j.pos=position(p);j.item=SurvivalRules.itemId(text(p,"item",""));
        if(j.item.isEmpty())throw new IllegalArgumentException("item is required");
        int source=find(player,j.item);if(source<0)throw new IllegalStateException("item is not in inventory: "+j.item);
        ItemStack stack=player.getInventory().getItem(source);
        if(!(stack.getItem() instanceof BlockItem blockItem))throw new IllegalArgumentException("item is not placeable as a block: "+j.item);
        Level level=player.level();
        if(!level.hasChunkAt(j.pos))throw new IllegalStateException("target chunk is not loaded");
        BlockState target=level.getBlockState(j.pos);
        if(!target.canBeReplaced())throw new IllegalStateException("target position is not replaceable");
        if(player.getBoundingBox().intersects(new AABB(j.pos)))throw new IllegalStateException("target position intersects the player; move aside before placing");
        if(player.getEyePosition().distanceTo(Vec3.atCenterOf(j.pos))>4.5)throw new IllegalStateException("target position is outside interaction reach");
        BlockHitResult hit=null;
        if(placementSite(player,j.pos)) {
            BlockPos support=j.pos.below();
            double top=level.getBlockState(support).getCollisionShape(level,support).max(Direction.Axis.Y);
            try{hit=placementHit(player,support,Direction.UP,new Vec3(support.getX()+.5,support.getY()+top-.01,support.getZ()+.5));}catch(IllegalStateException ignored){}
        }
        for(Direction face:Direction.values()) {
            if(hit!=null)break;
            BlockPos support=j.pos.relative(face.getOpposite());
            if(!level.hasChunkAt(support))continue;
            BlockState supportState=level.getBlockState(support);
            if(!supportState.isFaceSturdy(level,support,face))continue;
            Vec3 point=Vec3.atCenterOf(support).add(face.getStepX()*.5,face.getStepY()*.5,face.getStepZ()*.5);
            try{hit=placementHit(player,support,face,point);break;}catch(IllegalStateException ignored){}
        }
        if(hit==null)throw new IllegalStateException("no visible stable support face for placement");
        select(player,hotbar(player,source));player.lookAt(EntityAnchorArgument.Anchor.EYES,hit.getLocation());
        Minecraft.getInstance().gameMode.useItemOn(player,InteractionHand.MAIN_HAND,hit);player.swing(InteractionHand.MAIN_HAND);
        j.detail.add("position",xyz(j.pos));j.detail.addProperty("item",j.item);
        j.detail.addProperty("block",BuiltInRegistries.BLOCK.getKey(blockItem.getBlock()).toString());
        j.next=j.started+300;j.deadline=j.started+3000;
    }

    private void tickPlaceAt(Job j,LocalPlayer player,long now) {
        String expected=j.detail.has("block")?j.detail.get("block").getAsString():"";
        BlockState current=player.level().getBlockState(j.pos);
        if(!expected.isEmpty()&&blockId(current).equals(expected)){j.detail.addProperty("placed",true);finish(true,null);return;}
        if(now-j.started<1200)return;
        throw new IllegalStateException("placement was not confirmed; target is still empty or was replaced by another block");
    }

    private JsonObject scanBlocks(LocalPlayer player,JsonObject p) {
        int radius=Math.max(1,Math.min(16,integer(p,"radius",8)));
        int limit=Math.max(1,Math.min(32,integer(p,"limit",16)));
        boolean includeAir=p.has("include_air")&&p.get("include_air").getAsBoolean();
        String filter=text(p,"block","").trim();
        Set<String> filters=new HashSet<>();
        Set<TagKey<net.minecraft.world.level.block.Block>> tags=new HashSet<>();
        if(!filter.isEmpty())for(String part:filter.split("[,;\\s]+")) {
            if(part.isBlank())continue;
            try{
                if(part.startsWith("#"))tags.add(TagKey.create(Registries.BLOCK,ResourceLocation.parse(part.substring(1))));
                else filters.add(SurvivalRules.itemId(part));
            }catch(IllegalArgumentException ignored){}
        }
        BlockPos origin=player.blockPosition();Level level=player.level();
        List<JsonObject> rows=new ArrayList<>();
        LinkedHashMap<String,Integer> summary=new LinkedHashMap<>();
        int total=0;
        for(BlockPos mutable:BlockPos.betweenClosed(origin.offset(-radius,-radius,-radius),origin.offset(radius,radius,radius))) {
            BlockPos pos=mutable.immutable();if(!level.hasChunkAt(pos))continue;
            BlockState state=level.getBlockState(pos);String id=blockId(state);
            if(!includeAir&&state.isAir())continue;
            if(!filters.isEmpty()||!tags.isEmpty()) {
                boolean matched=filters.contains(id);
                for(TagKey<net.minecraft.world.level.block.Block> tag:tags)if(state.is(tag)){matched=true;break;}
                if(!matched)continue;
            }
            total++;
            summary.merge(id,1,Integer::sum);
            JsonObject row=xyz(pos);row.addProperty("block",id);
            row.addProperty("distance",Math.round(player.position().distanceTo(Vec3.atCenterOf(pos))*10)/10.0);
            if(level.getBlockEntity(pos)!=null)row.addProperty("block_entity",true);
            rows.add(row);
        }
        rows.sort(Comparator.comparingDouble(row->row.get("distance").getAsDouble()));
        JsonArray out=new JsonArray();for(int i=0;i<Math.min(limit,rows.size());i++)out.add(rows.get(i));
        List<java.util.Map.Entry<String,Integer>> counts=new ArrayList<>(summary.entrySet());
        counts.sort((a,b)->{int by=b.getValue()-a.getValue();return by!=0?by:a.getKey().compareTo(b.getKey());});
        JsonArray summaryJson=new JsonArray();
        for(int i=0;i<Math.min(20,counts.size());i++){JsonObject item=new JsonObject();item.addProperty("block",counts.get(i).getKey());item.addProperty("count",counts.get(i).getValue());summaryJson.add(item);}
        JsonObject result=new JsonObject();result.addProperty("count",out.size());result.addProperty("total",total);result.add("blocks",out);result.add("summary",summaryJson);result.addProperty("radius",radius);result.addProperty("include_air",includeAir);return result;
    }

    private JsonObject scanEntities(LocalPlayer player,JsonObject p) {
        int radius=Math.max(1,Math.min(32,integer(p,"radius",16)));
        int limit=Math.max(1,Math.min(32,integer(p,"limit",16)));
        String filter=text(p,"type","").trim();
        AABB box=player.getBoundingBox().inflate(radius);
        List<Entity> entities=player.level().getEntities(player,box,entity->{
            if(!entity.isAlive())return false;
            if(filter.isEmpty())return true;
            String type=BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).toString();
            return type.equals(filter)||entity.getName().getString().equalsIgnoreCase(filter)||entity.getUUID().toString().equals(filter);
        });
        entities.sort(Comparator.comparingDouble(entity->entity.distanceToSqr(player)));
        JsonArray out=new JsonArray();
        for(int i=0;i<Math.min(limit,entities.size());i++) {
            Entity entity=entities.get(i);JsonObject row=xyz(entity.blockPosition());
            row.addProperty("uuid",entity.getUUID().toString());
            String typeId=BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).toString();
            row.addProperty("type",typeId);
            row.addProperty("name",entity.getName().getString());
            row.addProperty("distance",entity.distanceTo(player));
            row.addProperty("hostile",entity instanceof Enemy&&SurvivalRules.hostileType(typeId,BridgeConfig.NON_HOSTILE_ENTITIES.get()));
            if(entity instanceof LivingEntity living){row.addProperty("health",living.getHealth());row.addProperty("max_health",living.getMaxHealth());}
            if(entity instanceof ItemEntity drop){row.add("item",stackInfo(drop.getItem()));row.addProperty("item_count",drop.getItem().getCount());}
            out.add(row);
        }
        JsonObject result=new JsonObject();result.addProperty("count",out.size());result.add("entities",out);result.addProperty("radius",radius);return result;
    }

    private JsonObject inspectBlock(LocalPlayer player,JsonObject p) {
        return inspectBlock(player,p,true);
    }

    private JsonObject inspectBlock(LocalPlayer player,JsonObject p,boolean requireReach) {
        BlockPos pos=position(p);Level level=player.level();
        if(!level.hasChunkAt(pos))throw new IllegalStateException("target chunk is not loaded");
        double distance=player.getEyePosition().distanceTo(Vec3.atCenterOf(pos));
        if(requireReach&&distance>16)throw new IllegalStateException("OUT_OF_RANGE: target is "+String.format(java.util.Locale.ROOT,"%.1f",distance)+" blocks away; use mc_goto first");
        BlockState state=level.getBlockState(pos);BlockEntity blockEntity=level.getBlockEntity(pos);
        JsonObject result=xyz(pos);result.addProperty("block",blockId(state));
        result.addProperty("state",state.toString());
        result.addProperty("air",state.isAir());
        result.addProperty("replaceable",state.canBeReplaced());
        result.addProperty("hardness",state.getDestroySpeed(level,pos));
        result.addProperty("requires_correct_tool",state.requiresCorrectToolForDrops());
        result.addProperty("harvestable_with_held",player.getMainHandItem().isCorrectToolForDrops(state));
        result.addProperty("block_entity",blockEntity!=null);
        if(blockEntity!=null)result.addProperty("block_entity_type",BuiltInRegistries.BLOCK_ENTITY_TYPE.getKey(blockEntity.getType()).toString());
        result.addProperty("distance",distance);
        result.addProperty("interactable",interactable(player,pos));
        result.addProperty("standable",standable(level,pos));
        return result;
    }

    private JsonObject blockContext(LocalPlayer player,JsonObject p) {
        BlockPos pos=position(p);Level level=player.level();
        if(!level.hasChunkAt(pos))throw new IllegalStateException("target chunk is not loaded");
        JsonObject result=inspectBlock(player,p,false);
        BlockState state=level.getBlockState(pos);
        boolean bed=state.getBlock() instanceof BedBlock;
        result.addProperty("bed",bed&&!state.getValue(BedBlock.OCCUPIED));
        result.addProperty("bed_occupied",bed&&state.getValue(BedBlock.OCCUPIED));
        result.addProperty("door",state.getBlock() instanceof DoorBlock);
        result.addProperty("container",level.getBlockEntity(pos) instanceof net.minecraft.world.Container);
        JsonArray stands=new JsonArray();
        for(Direction dir:Direction.Plane.HORIZONTAL)for(int dy=-1;dy<=0;dy++) {
            BlockPos stand=pos.relative(dir).offset(0,dy,0);
            if(standable(level,stand))stands.add(node(player,stand));
        }
        result.add("stands",stands);
        return result;
    }

    private JsonObject dropItem(LocalPlayer player,JsonObject p) {
        String requestedItem=text(p,"item","").trim();
        int requested=Math.max(1,Math.min(64,integer(p,"count",1)));
        int slot;
        String item;
        if(requestedItem.isEmpty()) {
            slot=player.getInventory().selected;
            ItemStack selectedStack=player.getInventory().getItem(slot);
            if(selectedStack.isEmpty())throw new IllegalStateException("no item is selected; provide item or select a hotbar slot first");
            item=BuiltInRegistries.ITEM.getKey(selectedStack.getItem()).toString();
        } else {
            item=SurvivalRules.itemId(requestedItem);
            slot=find(player,item);if(slot<0)throw new IllegalStateException("item is not in inventory: "+item);
        }
        ItemStack stack=player.getInventory().getItem(slot);
        int count=Math.min(requested,stack.getCount());
        ItemStack dropped=stack.split(count);player.drop(dropped,false);
        JsonObject result=new JsonObject();result.addProperty("item",item);result.addProperty("count",count);result.addProperty("dropped",true);return result;
    }

    private JsonObject selectSlot(LocalPlayer player,JsonObject p) {
        int slot=integer(p,"slot",-1);
        if(slot<0||slot>8)throw new IllegalArgumentException("hotbar slot must be 0..8");
        select(player,slot);
        JsonObject result=new JsonObject();result.addProperty("selected",player.getInventory().selected);return result;
    }

    private boolean matchesSortCategory(ItemStack stack,int target) {
        if(stack.isEmpty())return false;
        String id=BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
        return switch(target) {
            case 0 -> id.endsWith("_sword")||id.equals("minecraft:trident")||id.equals("minecraft:bow")||id.equals("minecraft:crossbow");
            case 1 -> id.endsWith("_pickaxe");
            case 2 -> id.endsWith("_axe");
            case 3 -> id.endsWith("_shovel");
            case 4 -> id.equals("minecraft:torch")||id.equals("minecraft:wall_torch");
            case 5 -> stack.getItem() instanceof BlockItem&&!id.equals("minecraft:torch")&&!id.equals("minecraft:wall_torch");
            case 6 -> {
                var food=stack.get(DataComponents.FOOD);
                yield food!=null&&food.effects().isEmpty();
            }
            case 7 -> id.endsWith("_hoe")||id.equals("minecraft:shears")||id.equals("minecraft:flint_and_steel")||id.equals("minecraft:fishing_rod");
            default -> false;
        };
    }

    private JsonObject sortInventory(LocalPlayer player) {
        if(Minecraft.getInstance().screen!=null)throw new IllegalStateException("close the current screen before sorting");
        if(!(player.containerMenu instanceof InventoryMenu))throw new IllegalStateException("sort requires the player inventory menu");
        if(!player.containerMenu.getCarried().isEmpty())throw new IllegalStateException("cursor is not empty; refusing to sort");
        JsonArray moves=new JsonArray();
        java.util.Set<Integer> used=new HashSet<>();
        for(int target=0;target<8;target++) {
            if(matchesSortCategory(player.getInventory().getItem(target),target))continue;
            int source=-1;
            for(int i=0;i<36;i++) {
                if(i==target||used.contains(i))continue;
                if(matchesSortCategory(player.getInventory().getItem(i),target)){source=i;break;}
            }
            if(source<0)continue;
            String item=BuiltInRegistries.ITEM.getKey(player.getInventory().getItem(source).getItem()).toString();
            click(player,slot(player,source).index,target,ClickType.SWAP);
            used.add(source);
            JsonObject move=new JsonObject();move.addProperty("from",source);move.addProperty("to",target);move.addProperty("item",item);moves.add(move);
        }
        JsonObject result=new JsonObject();result.addProperty("sorted",true);result.add("moves",moves);
        JsonArray hotbar=new JsonArray();for(int i=0;i<9;i++)hotbar.add(BuiltInRegistries.ITEM.getKey(player.getInventory().getItem(i).getItem()).toString());
        result.add("hotbar",hotbar);return result;
    }

    private Entity entityByRef(LocalPlayer player,String ref,double radius) {
        if(ref==null||ref.isBlank())return null;
        for(Entity entity:player.level().getEntities(player,player.getBoundingBox().inflate(radius),Entity::isAlive)) {
            if(entity.getUUID().toString().equals(ref)||entity.getName().getString().equalsIgnoreCase(ref)
                ||BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).toString().equals(ref))return entity;
        }
        return null;
    }

    private JsonObject useItem(LocalPlayer player,JsonObject p) {
        String item=SurvivalRules.itemId(text(p,"item",""));
        if(!item.isEmpty()) {
            int source=find(player,item);if(source<0)throw new IllegalStateException("item is not in inventory: "+item);
            select(player,hotbar(player,source));
        }
        JsonObject result=new JsonObject();
        result.addProperty("item",BuiltInRegistries.ITEM.getKey(player.getMainHandItem().getItem()).toString());
        String target=text(p,"target","").trim();
        Entity entity=entityByRef(player,target,8);
        if(entity!=null) {
            player.lookAt(EntityAnchorArgument.Anchor.EYES,entity.position().add(0,entity.getBbHeight()/2,0));
            Minecraft.getInstance().gameMode.interact(player,entity,InteractionHand.MAIN_HAND);player.swing(InteractionHand.MAIN_HAND);
            result.addProperty("target",entity.getUUID().toString());result.addProperty("target_type","entity");
        } else if(p.has("x")&&p.has("y")&&p.has("z")) {
            BlockPos pos=position(p);BlockHitResult hit=visible(player,pos);
            player.lookAt(EntityAnchorArgument.Anchor.EYES,hit.getLocation());
            Minecraft.getInstance().gameMode.useItemOn(player,InteractionHand.MAIN_HAND,hit);player.swing(InteractionHand.MAIN_HAND);
            result.add("position",xyz(pos));result.addProperty("target_type","block");
        } else {
            Minecraft.getInstance().gameMode.useItem(player,InteractionHand.MAIN_HAND);player.swing(InteractionHand.MAIN_HAND);
            result.addProperty("target_type","self");
        }
        result.addProperty("used",true);return result;
    }

    private JsonObject moveItem(LocalPlayer player,JsonObject p) {
        Minecraft mc=Minecraft.getInstance();
        if(!(mc.screen instanceof AbstractContainerScreen<?> screen))throw new IllegalStateException("no container screen is open");
        AbstractContainerMenu menu=screen.getMenu();
        if(menu instanceof InventoryMenu||menu instanceof CraftingMenu||menu instanceof MerchantMenu)
            throw new IllegalStateException("move_item requires a storage container screen");
        if(!menu.getCarried().isEmpty())throw new IllegalStateException("cursor is not empty; refusing to move items");
        String item=SurvivalRules.itemId(text(p,"item",""));
        if(item.isEmpty())throw new IllegalArgumentException("item is required");
        var registryItem=BuiltInRegistries.ITEM.get(ResourceLocation.parse(item));
        if(registryItem==Items.AIR)throw new IllegalArgumentException("unknown item: "+item);
        String direction=text(p,"direction","to_container").trim().toLowerCase();
        if(!direction.equals("to_container")&&!direction.equals("from_container"))
            throw new IllegalArgumentException("direction must be to_container or from_container");
        int requested=SurvivalRules.checkedCount(integer(p,"count",1));
        boolean toContainer=direction.equals("to_container");
        ItemStack probe=new ItemStack(registryItem);
        List<Slot> sources=new ArrayList<>(),destinations=new ArrayList<>();
        for(Slot slot:menu.slots) {
            boolean playerSlot=slot.container instanceof Inventory;
            String id=BuiltInRegistries.ITEM.getKey(slot.getItem().getItem()).toString();
            if(((toContainer&&playerSlot)||(!toContainer&&!playerSlot))&&id.equals(item))sources.add(slot);
            if((toContainer?!playerSlot:playerSlot)&&slot.mayPlace(probe))destinations.add(slot);
        }
        if(sources.isEmpty())throw new IllegalStateException("item is not in the selected side: "+item);
        int remaining=requested,moved=0;
        JsonArray moves=new JsonArray();
        for(Slot source:sources) {
            if(remaining<=0)break;
            ItemStack stack=source.getItem();
            if(stack.isEmpty()||!BuiltInRegistries.ITEM.getKey(stack.getItem()).toString().equals(item))continue;
            int take=Math.min(remaining,stack.getCount());
            Slot destination=null;int space=0;
            for(Slot candidate:destinations) {
                space=sortSpace(candidate,item,probe.getMaxStackSize());
                if(space>0){destination=candidate;break;}
            }
            if(destination==null)break;
            take=Math.min(take,space);
            if(take<=0)break;
            click(player,source.index,0,ClickType.PICKUP);
            if(menu.getCarried().isEmpty())throw new IllegalStateException("server did not pick up the source stack");
            if(take==menu.getCarried().getCount())click(player,destination.index,0,ClickType.PICKUP);
            else {
                for(int i=0;i<take;i++)click(player,destination.index,1,ClickType.PICKUP);
                if(!menu.getCarried().isEmpty())click(player,source.index,0,ClickType.PICKUP);
            }
            if(!menu.getCarried().isEmpty())throw new IllegalStateException("cursor is not empty after transfer; stop and inspect");
            JsonObject move=new JsonObject();move.addProperty("from",source.index);move.addProperty("to",destination.index);move.addProperty("count",take);moves.add(move);
            moved+=take;remaining-=take;
        }
        JsonObject result=new JsonObject();result.addProperty("item",item);
        result.addProperty("direction",direction);result.addProperty("requested",requested);
        result.addProperty("moved",moved);result.addProperty("complete",moved>=requested);result.add("moves",moves);
        return result;
    }

    private static int sortSpace(Slot slot,String item,int maxStack) {
        ItemStack stack=slot.getItem();
        if(stack.isEmpty())return maxStack;
        if(!BuiltInRegistries.ITEM.getKey(stack.getItem()).toString().equals(item))return 0;
        return Math.max(0,Math.min(stack.getMaxStackSize(),maxStack)-stack.getCount());
    }

    private JsonObject trade(LocalPlayer player,JsonObject p) {
        Minecraft mc=Minecraft.getInstance();
        if(!(player.containerMenu instanceof MerchantMenu menu))throw new IllegalStateException("no merchant screen is open");
        String operation=text(p,"operation","read").trim().toLowerCase();
        if(operation.equals("read")) {
            JsonObject result=new JsonObject();result.addProperty("operation","read");
            JsonArray offers=new JsonArray();
            for(int i=0;i<menu.getOffers().size();i++) {
                var offer=menu.getOffers().get(i);JsonObject row=new JsonObject();
                row.addProperty("index",i);row.add("result",stackInfo(offer.getResult()));
                row.add("cost_a",stackInfo(offer.getCostA()));row.add("cost_b",stackInfo(offer.getCostB()));
                row.addProperty("uses",offer.getUses());row.addProperty("max_uses",offer.getMaxUses());
                row.addProperty("xp",offer.getXp());row.addProperty("out_of_stock",offer.isOutOfStock());
                offers.add(row);
            }
            result.add("offers",offers);return result;
        }
        if(operation.equals("trade")) {
            int index=integer(p,"index",-1);
            if(index<0||index>=menu.getOffers().size())throw new IllegalArgumentException("offer index is outside the merchant list");
            if(!menu.getCarried().isEmpty())throw new IllegalStateException("cursor is not empty; refusing to trade");
            menu.setSelectionHint(index);menu.tryMoveItems(index);
            if(mc.getConnection()!=null)mc.getConnection().send(new ServerboundSelectTradePacket(index));
            Slot output=menu.getSlot(2);
            if(output.getItem().isEmpty())throw new IllegalStateException("offer unavailable or missing payment items");
            ItemStack offered=output.getItem().copy();
            click(player,output.index,0,ClickType.QUICK_MOVE);
            JsonObject result=new JsonObject();result.addProperty("operation","trade");result.addProperty("index",index);
            result.add("result",stackInfo(offered));result.addProperty("traded",offered.getCount());
            result.addProperty("cursor_empty",menu.getCarried().isEmpty());return result;
        }
        if(operation.equals("close")) {
            player.closeContainer();if(mc.screen!=null)mc.setScreen(null);
            JsonObject result=new JsonObject();result.addProperty("operation","close");result.addProperty("closed",true);return result;
        }
        throw new IllegalArgumentException("trade operation must be read, trade or close");
    }

    private JsonObject ride(LocalPlayer player,JsonObject p) {
        if(player.getVehicle()!=null)throw new IllegalStateException("player is already riding something");
        String target=text(p,"target","").trim();
        if(target.isEmpty())throw new IllegalArgumentException("target entity is required");
        Entity entity=entityByRef(player,target,8);
        if(entity==null)throw new IllegalArgumentException("target entity is not nearby: "+target);
        player.lookAt(EntityAnchorArgument.Anchor.EYES,entity.position().add(0,entity.getBbHeight()/2,0));
        Minecraft.getInstance().gameMode.interact(player,entity,InteractionHand.MAIN_HAND);player.swing(InteractionHand.MAIN_HAND);
        JsonObject result=new JsonObject();result.addProperty("target",entity.getUUID().toString());
        result.addProperty("type",BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).toString());
        result.addProperty("requested",true);return result;
    }

    private JsonObject wake(LocalPlayer player) {
        JsonObject result=new JsonObject();
        if(!player.isSleeping()){result.addProperty("sleeping",false);result.addProperty("awake",true);return result;}
        player.connection.send(new ServerboundPlayerCommandPacket(player,ServerboundPlayerCommandPacket.Action.STOP_SLEEPING));
        if(player.isSleeping())player.stopSleeping();
        result.addProperty("sleeping",false);result.addProperty("awake",true);return result;
    }

    private void beginDismount(Job j,LocalPlayer player) {
        if(!player.isPassenger())throw new IllegalStateException("player is not riding anything");
        player.input.shiftKeyDown=true;
        player.connection.send(new ServerboundPlayerInputPacket(0,0,false,true));
        j.stage=0;j.deadline=j.started+1600;j.next=j.started;
    }

    private void tickDismount(Job j,LocalPlayer player,long now) {
        if(!player.isPassenger()){player.input.shiftKeyDown=false;j.detail.addProperty("dismounted",true);finish(true,null);return;}
        if(now>j.deadline){player.input.shiftKeyDown=false;throw new IllegalStateException("dismount was not confirmed");}
        player.input.shiftKeyDown=true;
        player.connection.send(new ServerboundPlayerInputPacket(0,0,false,true));
        j.next=now+100;
    }

    private JsonObject container(LocalPlayer player,JsonObject p) {
        String operation=text(p,"operation","read").trim().toLowerCase();
        Minecraft mc=Minecraft.getInstance();
        if(operation.equals("open")) {
            if(job!=null)throw new IllegalStateException("another survival action is running; stop it first");
            BlockPos pos=position(p);visible(player,pos);useBlock(player,pos);
            JsonObject result=new JsonObject();result.addProperty("operation","open");result.add("position",xyz(pos));
            result.addProperty("screen",mc.screen==null?"none":mc.screen.getClass().getSimpleName());return result;
        }
        if(!(mc.screen instanceof AbstractContainerScreen<?> screen))throw new IllegalStateException("no container screen is open");
        AbstractContainerMenu menu=screen.getMenu();
        if(operation.equals("read")) {
            JsonArray slots=new JsonArray();
            for(int i=0;i<menu.slots.size();i++) {
                Slot slot=menu.slots.get(i);JsonObject row=stackInfo(slot.getItem());
                row.addProperty("slot",i);row.addProperty("player_inventory",slot.container instanceof net.minecraft.world.entity.player.Inventory);
                slots.add(row);
            }
            JsonObject result=new JsonObject();result.addProperty("operation","read");result.addProperty("window_id",menu.containerId);
            result.addProperty("title",screen.getTitle().getString());result.add("slots",slots);return result;
        }
        if(operation.equals("click")) {
            if(job!=null)throw new IllegalStateException("another survival action is running; stop it first");
            int slot=integer(p,"slot",-1);if(slot<0||slot>=menu.slots.size())throw new IllegalArgumentException("slot is outside the open container");
            int button=Math.max(0,Math.min(8,integer(p,"button",0)));
            String mode=text(p,"mode","pickup").trim().toLowerCase();
            ClickType kind=switch(mode){case "quick_move","shift"->ClickType.QUICK_MOVE;case "swap"->ClickType.SWAP;case "throw"->ClickType.THROW;case "clone"->ClickType.CLONE;case "pickup_all"->ClickType.PICKUP_ALL;default->ClickType.PICKUP;};
            click(player,slot,button,kind);
            JsonObject result=stackInfo(menu.slots.get(slot).getItem());result.addProperty("operation","click");result.addProperty("slot",slot);result.addProperty("mode",mode);return result;
        }
        if(operation.equals("close")) {
            player.closeContainer();if(mc.screen!=null)mc.setScreen(null);
            JsonObject result=new JsonObject();result.addProperty("operation","close");result.addProperty("closed",true);return result;
        }
        throw new IllegalArgumentException("container operation must be open, read, click or close");
    }

    private static String blockId(BlockState state) {return BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString();}

    private boolean natural(LocalPlayer player,BlockPos pos,String resource) {
        Level level=player.level();if(!level.hasChunkAt(pos))return false;
        BlockState s=level.getBlockState(pos);if(level.getBlockEntity(pos)!=null||!s.getFluidState().isEmpty())return false;
        String key=BuiltInRegistries.BLOCK.getKey(s.getBlock()).toString();
        if(!((resource.equals("logs")&&s.is(BlockTags.LOGS))||(resource.equals("stone")&&STONES.contains(key))
            ||(resource.equals("leaves")&&s.is(BlockTags.LEAVES))||(resource.equals("soil")&&(s.is(Blocks.DIRT)||s.is(Blocks.GRASS_BLOCK)))||(resource.equals("coal")&&COAL.contains(key))||(resource.equals("iron")&&IRON.contains(key))||(resource.equals("wheat")&&s.is(Blocks.WHEAT))))return false;
        for(BlockPos near:BlockPos.betweenClosed(pos.offset(-2,-1,-2),pos.offset(2,2,2))) {
            if(!level.hasChunkAt(near))return false;
            BlockState n=level.getBlockState(near);String nearbyKey=BuiltInRegistries.BLOCK.getKey(n.getBlock()).toString();
            if(level.getBlockEntity(near)!=null||n.is(Blocks.CRAFTING_TABLE)||n.is(BlockTags.PLANKS)||n.getBlock() instanceof BedBlock||nearbyKey.contains("bricks"))return false;
        }
        // Narrow clearance only: one nonpersistent leaf beside a validated natural tree.
        if(resource.equals("soil")) {
            // Only a single soft surface layer immediately over a validated stone seam.
            if(!STONES.contains(BuiltInRegistries.BLOCK.getKey(level.getBlockState(pos.below()).getBlock()).toString())||!natural(player,pos.below(),"stone"))return false;
            if(!passable(level,pos.above())||!passable(level,pos.above(2)))return false;
            for(BlockPos near:BlockPos.betweenClosed(pos.offset(-1,-1,-1),pos.offset(1,1,1))) {
                BlockState n=level.getBlockState(near);
                if(!n.getFluidState().isEmpty()||n.is(Blocks.FARMLAND)||n.is(Blocks.DIRT_PATH)||n.is(BlockTags.LOGS)
                    ||n.is(Blocks.SAND)||n.is(Blocks.GRAVEL)||n.getBlock() instanceof CropBlock)return false;
            }
            return true;
        }
        if(resource.equals("leaves")) {
            if(!(s.getBlock() instanceof LeavesBlock)||s.getValue(LeavesBlock.PERSISTENT))return false;
            for(BlockPos near:BlockPos.betweenClosed(pos.offset(-2,-1,-2),pos.offset(2,1,2)))
                if(level.hasChunkAt(near)&&level.getBlockState(near).is(BlockTags.LOGS)&&natural(player,near,"logs"))return true;
            return false;
        }
        if(resource.equals("stone"))return STONES.contains(key);
        if(resource.equals("coal"))return COAL.contains(key);
        if(resource.equals("iron"))return IRON.contains(key);
        if(resource.equals("wheat"))return s.getBlock()==Blocks.WHEAT&&((CropBlock)s.getBlock()).isMaxAge(s);
        if(!resource.equals("logs")||!s.is(BlockTags.LOGS))return false;
        boolean grounded=false,leaves=false;
        for(int down=1;down<=12;down++) {
            BlockState under=level.getBlockState(pos.below(down));
            if(SOIL.contains(BuiltInRegistries.BLOCK.getKey(under.getBlock()).toString())){grounded=true;break;}
            if(!under.isAir()&&!under.is(BlockTags.LOGS))break;
        }
        if(!grounded)return false;
        for(BlockPos near:BlockPos.betweenClosed(pos.offset(-3,0,-3),pos.offset(3,6,3)))
            if(level.hasChunkAt(near)&&level.getBlockState(near).is(BlockTags.LEAVES)){leaves=true;break;}
        return leaves;
    }
    private JsonObject scan(LocalPlayer player,JsonObject p) {
        return scan(player,p,12);
    }
    private JsonObject scan(LocalPlayer player,JsonObject p,int maxRadius) {
        int radius=Math.max(1,Math.min(maxRadius,integer(p,"radius",8)));String resource=text(p,"resource","logs");
        boolean resourcesOnly=p.has("native_resources_only")&&p.get("native_resources_only").getAsBoolean();
        List<JsonObject> candidates=new ArrayList<>(),tables=new ArrayList<>(),beds=new ArrayList<>(),doors=new ArrayList<>(),ground=new ArrayList<>(),placements=new ArrayList<>(),stations=new ArrayList<>(),quarries=new ArrayList<>();
        BlockPos origin=player.blockPosition();Level level=player.level();
        if(!p.has("target_only")||!p.get("target_only").getAsBoolean())for(BlockPos mutable:BlockPos.betweenClosed(origin.offset(-radius,-3,-radius),origin.offset(radius,4,radius))) {
            BlockPos pos=mutable.immutable();if(!level.hasChunkAt(pos))continue;
            BlockState state=level.getBlockState(pos);
            if(state.is(Blocks.CRAFTING_TABLE))tables.add(node(player,pos));
            String blockId=BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString();
            if(Set.of("minecraft:furnace","farmersdelight:cutting_board","minecraft:torch").contains(blockId)) {
                JsonObject station=node(player,pos);station.addProperty("item",blockId);stations.add(station);
            }
            if(state.getBlock() instanceof BedBlock&&!state.getValue(BedBlock.OCCUPIED))beds.add(node(player,pos));
            if(state.getBlock() instanceof DoorBlock&&state.getValue(DoorBlock.HALF)==DoubleBlockHalf.LOWER)doors.add(node(player,pos));
            boolean exposed=false;
            if(Set.of("stone","coal","iron").contains(resource)) {
                for(Direction side:Direction.values())if(passable(level,pos.relative(side))){exposed=true;break;}
            } else exposed=true;
            if(exposed&&pos.getY()>=origin.getY()-2&&SurvivalRules.safeMiningCell(pos.getX(),pos.getY(),pos.getZ(),player.getX(),player.getY(),player.getZ())&&!state.isAir()&&natural(player,pos,resource))candidates.add(xyzDistance(player,pos));
            if(resource.equals("stone")&&pos.getY()==origin.getY()-1&&pos.distSqr(origin)<=64&&natural(player,pos,"soil")) {
                for(Direction side:Direction.Plane.HORIZONTAL) {
                    BlockPos work=pos.relative(side);if(!natural(player,work,"soil"))continue;
                    for(Direction outside:Direction.Plane.HORIZONTAL) {
                        BlockPos stand=pos.relative(outside).above();
                        if(stand.equals(work.above())||!standable(level,stand))continue;
                        JsonObject q=xyzDistance(player,pos);q.add("stand",xyz(stand));q.add("entry",xyz(pos));q.add("work",xyz(work));quarries.add(q);break;
                    }
                    if(!quarries.isEmpty())break;
                }
            }
            double horizontal=Math.hypot(pos.getX()+.5-player.getX(),pos.getZ()+.5-player.getZ());
            boolean groundCandidate=pos.getX()%4==0&&pos.getZ()%4==0&&horizontal>=4&&!level.getBlockState(pos.below()).is(BlockTags.LEAVES);
            boolean placementCandidate=!resourcesOnly&&SurvivalRules.sameBuildLevel(pos.getY(),player.getY())&&horizontal>1.5&&horizontal<3.5;
            if((groundCandidate||placementCandidate)&&standable(level,pos)){
                if(groundCandidate)ground.add(node(player,pos));
                if(placementCandidate&&placementSite(player,pos))placements.add(node(player,pos));
            }
        }
        Comparator<JsonObject> order=Comparator.comparingDouble(a->a.get("distance").getAsDouble());
        JsonArray resources=new JsonArray();candidates.sort(order);
        // Enrich a bounded set, rather than ray-casting every block in the scan cube.
        for(int i=0;i<Math.min(24,candidates.size());i++)resources.add(resourceNode(player,position(candidates.get(i))));
        JsonObject result=new JsonObject();result.addProperty("planning_version",1);result.add("resources",resources);result.add("workbenches",limited(tables,order,8));result.add("beds",limited(beds,order,8));result.add("doors",limited(doors,order,8));result.add("ground",limited(ground,order,24));
        result.add("placements",limited(placements,order,16));
        result.add("stations",limited(stations,order,12));
        result.add("quarry_sites",limited(quarries,order,8));
        if(p.has("x")&&p.has("y")&&p.has("z")) {
            BlockPos target=position(p);
            if(!level.hasChunkAt(target)||target.distSqr(origin)>32*32)throw new IllegalArgumentException("target is outside loaded local observation range");
            result.addProperty("target_standable",standable(level,target));
            result.add("target_node",resourceNode(player,target));
            result.addProperty("target_block",BuiltInRegistries.BLOCK.getKey(level.getBlockState(target).getBlock()).toString());
            result.addProperty("target_interactable",interactable(player,target));
            result.addProperty("target_clearable",natural(player,target,"leaves")&&target.getY()>=origin.getY());
            result.addProperty("target_quarry_clearable",natural(player,target,"soil"));
            result.add("pickup_paths",pickupPaths(player,target));
            JsonArray drops=new JsonArray();
            for(ItemEntity item:level.getEntitiesOfClass(ItemEntity.class,new AABB(target.getX()-3,target.getY()-12,target.getZ()-3,target.getX()+4,target.getY()+4,target.getZ()+4))) {
                if(!item.isAlive())continue;
                String itemId=BuiltInRegistries.ITEM.getKey(item.getItem().getItem()).toString();
                boolean matches=switch(resource) {
                    case "logs" -> itemId.endsWith("_log");
                    case "stone" -> Set.of("minecraft:cobblestone","minecraft:cobbled_deepslate","minecraft:stone","minecraft:deepslate").contains(itemId);
                    case "coal" -> itemId.equals("minecraft:coal");
                    case "iron" -> Set.of("minecraft:raw_iron","minecraft:iron_ore","minecraft:deepslate_iron_ore").contains(itemId);
                    case "wheat" -> itemId.equals("minecraft:wheat");
                    default -> true;
                };
                if(!matches)continue;
                JsonObject drop=stackInfo(item.getItem());drop.add("position",xyz(item.blockPosition()));
                drop.addProperty("pickup_delayed",item.hasPickUpDelay());
                drop.addProperty("entity_uuid",item.getUUID().toString());
                JsonObject precise=new JsonObject();precise.addProperty("x",item.getX());precise.addProperty("y",item.getY());precise.addProperty("z",item.getZ());drop.add("precise_position",precise);
                drop.add("pickup_paths",pickupPaths(player,item.blockPosition()));drops.add(drop);
                if(drops.size()>=16)break;
            }
            result.add("drops",drops);
        }
        return result;
    }
    private JsonArray limited(List<JsonObject> rows,Comparator<JsonObject> order,int limit) {
        rows.sort(order);JsonArray array=new JsonArray();for(int i=0;i<Math.min(limit,rows.size());i++)array.add(rows.get(i));return array;
    }
    private JsonObject node(LocalPlayer player,BlockPos pos) {
        JsonObject n=xyzDistance(player,pos);n.addProperty("interactable",interactable(player,pos));
        JsonArray stands=new JsonArray();
        for(Direction dir:Direction.Plane.HORIZONTAL)for(int dy=-1;dy<=0;dy++){
            BlockPos stand=pos.relative(dir).offset(0,dy,0);if(standable(player.level(),stand))stands.add(xyzDistance(player,stand));
        }
        n.add("stands",stands);return n;
    }
    private boolean standable(Level level,BlockPos pos) {
        LocalPlayer player=Minecraft.getInstance().player;
        return player!=null&&player.level()==level&&Double.isFinite(TerrainQueries.height(player,pos,Set.of()));
    }
    private JsonObject xyzDistance(LocalPlayer player,BlockPos pos) {
        JsonObject n=xyz(pos);n.addProperty("distance",player.position().distanceTo(Vec3.atCenterOf(pos)));
        double feet=TerrainQueries.height(player,pos,Set.of());if(Double.isFinite(feet))n.addProperty("feet_y",feet);return n;
    }
    private boolean passable(Level level,BlockPos pos) {
        BlockState s=level.getBlockState(pos);
        return s.getFluidState().isEmpty()&&(s.isAir()||s.is(Blocks.WHEAT)||s.is(Blocks.SHORT_GRASS)||s.is(Blocks.TALL_GRASS)
            ||s.is(Blocks.FERN)||s.is(Blocks.LARGE_FERN)||s.is(Blocks.DEAD_BUSH)||(s.is(BlockTags.FLOWERS)&&!s.is(Blocks.WITHER_ROSE)));
    }
    private boolean safeSupport(Level level,BlockPos pos) {
        BlockState s=level.getBlockState(pos);
        return s.getFluidState().isEmpty()&&(s.isCollisionShapeFullBlock(level,pos)||s.is(Blocks.FARMLAND)||s.is(Blocks.DIRT_PATH))
            &&!s.is(Blocks.MAGMA_BLOCK)&&!s.is(Blocks.CAMPFIRE)&&!s.is(Blocks.SOUL_CAMPFIRE)&&!s.is(Blocks.CACTUS);
    }
    private boolean interactable(LocalPlayer player,BlockPos pos) {
        try{visible(player,pos);return true;}catch(IllegalStateException ex){return false;}
    }
    private boolean placementSite(LocalPlayer player,BlockPos pos) {
        Level level=player.level();BlockPos below=pos.below();
        if(!level.hasChunkAt(pos)||!level.getBlockState(pos).isAir()||!level.getBlockState(pos.above()).isAir()
            ||!safeSupport(level,below)||!level.getBlockState(below).isFaceSturdy(level,below,Direction.UP)
            ||level.getBlockState(below).is(BlockTags.LEAVES)||!level.noCollision(player,new AABB(pos))
            ||player.getBoundingBox().intersects(new AABB(pos)))return false;
        double top=level.getBlockState(below).getCollisionShape(level,below).max(Direction.Axis.Y);
        if(!SurvivalRules.keepsAccess(reachableCells(player,null),reachableCells(player,pos)))return false;
        try{visibleAt(player,below,new Vec3(below.getX()+.5,below.getY()+top-.01,below.getZ()+.5));return true;}
        catch(IllegalStateException ex){return false;}
    }
    private int reachableCells(LocalPlayer player,BlockPos blocked) {
        BlockPos start=player.blockPosition();Set<BlockPos> visited=new HashSet<>();var queue=new java.util.ArrayDeque<BlockPos>();
        visited.add(start);queue.add(start);
        while(!queue.isEmpty()&&visited.size()<384) {
            BlockPos current=queue.remove();
            for(Direction d:Direction.Plane.HORIZONTAL)for(int dy=-1;dy<=1;dy++) {
                BlockPos next=current.relative(d).offset(0,dy,0);
                if(Math.abs(next.getX()-start.getX())>4||Math.abs(next.getZ()-start.getZ())>4||Math.abs(next.getY()-start.getY())>3||visited.contains(next))continue;
                if(blocked!=null&&(next.equals(blocked)||next.above().equals(blocked)||next.below().equals(blocked)))continue;
                if(!standable(player.level(),next))continue;
                if(dy>0&&!passable(player.level(),current.above(2)))continue;
                if(dy<0&&!passable(player.level(),next.above(2)))continue;
                visited.add(next);queue.add(next);
            }
        }
        return visited.size();
    }
    private JsonObject resourceNode(LocalPlayer player,BlockPos pos) {
        JsonObject result=node(player,pos);List<JsonObject> plans=new ArrayList<>();
        String targetId=BuiltInRegistries.BLOCK.getKey(player.level().getBlockState(pos).getBlock()).toString();
        result.addProperty("block",targetId);
        boolean ore=STONES.contains(targetId)||COAL.contains(targetId)||IRON.contains(targetId);
        for(int dx=-3;dx<=3;dx++)for(int dz=-3;dz<=3;dz++)for(int dy=-2;dy<=1;dy++) {
            BlockPos stand=pos.offset(dx,dy,dz);
            if(ore&&stand.getY()<pos.getY()-1)continue;
            if((dx==0&&dz==0)||!standable(player.level(),stand))continue;
            Vec3 eye=new Vec3(stand.getX()+.5,TerrainQueries.height(player,stand,Set.of())+player.getEyeHeight(),stand.getZ()+.5);
            if(eye.distanceTo(Vec3.atCenterOf(pos))>4.3)continue;
            JsonArray clear=sightClearance(player,eye,pos);
            if(clear==null)continue;
            JsonObject plan=xyzDistance(player,stand);plan.add("clear",clear);plans.add(plan);
        }
        result.add("approaches",limited(plans,Comparator.comparingDouble(a->a.getAsJsonArray("clear").size()*5+a.get("distance").getAsDouble()
            +(ore?Math.max(0,Math.hypot(a.get("x").getAsInt()-pos.getX(),a.get("z").getAsInt()-pos.getZ())-1.5)*6:0)),12));
        return result;
    }
    private JsonArray sightClearance(LocalPlayer player,Vec3 eye,BlockPos target) {
        Level level=player.level();Set<BlockPos> ignored=new HashSet<>();JsonArray clear=new JsonArray();
        for(int i=0;i<=3;i++) {
            ClipContext context=new ClipContext(eye,Vec3.atCenterOf(target),ClipContext.Block.OUTLINE,ClipContext.Fluid.NONE,player) {
                @Override public VoxelShape getBlockShape(BlockState s,BlockGetter world,BlockPos p) {
                    return ignored.contains(p)?Shapes.empty():super.getBlockShape(s,world,p);
                }
            };
            BlockHitResult hit=level.clip(context);
            if(hit.getType()==HitResult.Type.BLOCK&&hit.getBlockPos().equals(target))return clear;
            BlockPos leaf=hit.getBlockPos().immutable();
            if(i==3||hit.getType()!=HitResult.Type.BLOCK||eye.distanceTo(hit.getLocation())>4.5
                ||leaf.getY()<Math.floor(eye.y-player.getEyeHeight())||!natural(player,leaf,"leaves"))return null;
            ignored.add(leaf);clear.add(xyz(leaf));
        }
        return null;
    }
    private JsonArray pickupPaths(LocalPlayer player,BlockPos target) {
        List<JsonObject> plans=new ArrayList<>();Level level=player.level();
        for(int dx=-1;dx<=1;dx++)for(int dz=-1;dz<=1;dz++)for(int dy=-1;dy<=0;dy++) {
            if(dx*dx+dz*dz>1)continue;
            BlockPos step=target.offset(dx,dy,dz);
            if(localStandable(player,step,Set.of())){JsonObject direct=xyzDistance(player,step);direct.add("clear",new JsonArray());plans.add(direct);continue;}
            if(!safeSupport(level,step.below()))continue;
            JsonArray clear=new JsonArray();boolean valid=true;
            for(BlockPos cell:List.of(step,step.above())) {
                if(passable(level,cell))continue;
                if(cell.getY()<player.blockPosition().getY()||!natural(player,cell,"leaves")){valid=false;break;}
                clear.add(xyz(cell));
            }
            if(!valid)continue;
            JsonObject plan=xyzDistance(player,step);plan.add("clear",clear);plans.add(plan);
        }
        return limited(plans,Comparator.comparingDouble(a->a.getAsJsonArray("clear").size()*5+a.get("distance").getAsDouble()),6);
    }
    private static JsonObject xyz(BlockPos pos) {JsonObject o=new JsonObject();o.addProperty("x",pos.getX());o.addProperty("y",pos.getY());o.addProperty("z",pos.getZ());return o;}
    private static BlockPos position(JsonObject p) {return new BlockPos(integer(p,"x",30000001),integer(p,"y",-9999),integer(p,"z",30000001));}
    private static String text(JsonObject p,String key,String fallback) {return p.has(key)?p.get(key).getAsString():fallback;}
    private static int integer(JsonObject p,String key,int fallback) {return p.has(key)?p.get(key).getAsInt():fallback;}
    private static int find(LocalPlayer player,String id) {for(int i=0;i<36;i++)if(!player.getInventory().getItem(i).isEmpty()&&BuiltInRegistries.ITEM.getKey(player.getInventory().getItem(i).getItem()).toString().equals(id))return i;return -1;}
    private static int count(LocalPlayer player,String id) {int count=0;for(int i=0;i<36;i++){ItemStack s=player.getInventory().getItem(i);if(BuiltInRegistries.ITEM.getKey(s.getItem()).toString().equals(id))count+=s.getCount();}return count;}
}
