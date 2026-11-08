package com.astrbot.bridge;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.util.Comparator;
import net.minecraft.client.Minecraft;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.level.LightLayer;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.EntityHitResult;

/** Structured local observations accompanying the actual render capture. */
final class GameSense {
    private GameSense() {}
    static JsonObject observe(LocalPlayer player) {
        Minecraft mc=Minecraft.getInstance();JsonObject result=new JsonObject();
        result.addProperty("captured_at_ms",System.currentTimeMillis());result.addProperty("game_tick",player.level().getGameTime());
        result.addProperty("yaw",player.getYRot());result.addProperty("pitch",player.getXRot());
        result.addProperty("in_water",player.isInWater());result.addProperty("on_fire",player.isOnFire());
        result.addProperty("air",player.getAirSupply());result.addProperty("on_ground",player.onGround());
        result.addProperty("raining",player.level().isRaining());result.addProperty("sky_visible",player.level().canSeeSky(player.blockPosition()));
        result.addProperty("block_light",player.level().getBrightness(LightLayer.BLOCK,player.blockPosition()));
        result.addProperty("biome",player.level().getBiome(player.blockPosition()).unwrapKey().map(k->k.location().toString()).orElse("unknown"));
        result.addProperty("held_id",BuiltInRegistries.ITEM.getKey(player.getMainHandItem().getItem()).toString());
        result.addProperty("screen",mc.screen==null?"none":mc.screen.getClass().getSimpleName());
        if(mc.hitResult instanceof BlockHitResult hit)result.add("crosshair",block(player,hit.getBlockPos()));
        else if(mc.hitResult instanceof EntityHitResult hit) {
            JsonObject entity=new JsonObject();entity.addProperty("entity",BuiltInRegistries.ENTITY_TYPE.getKey(hit.getEntity().getType()).toString());entity.addProperty("name",hit.getEntity().getName().getString());result.add("crosshair",entity);
        }
        JsonArray nearby=new JsonArray();
        for(var entity:player.level().getEntities(player,player.getBoundingBox().inflate(12),e->e!=player).stream().sorted(Comparator.comparingDouble(e->e.distanceToSqr(player))).limit(8).toList()) {
            JsonObject row=new JsonObject();row.addProperty("id",BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType()).toString());row.addProperty("name",entity.getName().getString());
            row.addProperty("distance",Math.round(entity.distanceTo(player)*10)/10.0);row.addProperty("line_of_sight",player.hasLineOfSight(entity));nearby.add(row);
        }
        result.add("nearby_entities",nearby);return result;
    }
    static JsonObject block(LocalPlayer player,BlockPos pos) {
        if(!player.level().hasChunkAt(pos)||pos.distToCenterSqr(player.position())>16*16)throw new IllegalArgumentException("block is outside loaded local perception");
        var state=player.level().getBlockState(pos);JsonObject row=new JsonObject();
        row.addProperty("id",BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString());row.addProperty("name",state.getBlock().getName().getString());
        row.addProperty("x",pos.getX());row.addProperty("y",pos.getY());row.addProperty("z",pos.getZ());
        row.addProperty("has_block_entity",player.level().getBlockEntity(pos)!=null);row.addProperty("state",state.toString());return row;
    }
}
