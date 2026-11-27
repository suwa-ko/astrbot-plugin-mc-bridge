package com.astrbot.bridge;

import com.google.gson.JsonObject;
import java.net.http.WebSocket;
import net.minecraft.core.BlockPos;
import net.minecraft.core.Direction;
import net.minecraft.world.entity.item.ItemEntity;
import net.minecraft.world.item.crafting.RecipeHolder;
import net.minecraft.world.level.Level;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.phys.Vec3;

/** State holder for a single tick-driven client operation. */
final class OperationJob {
    String id, kind, item = "";
    long generation, started = System.currentTimeMillis(), next, deadline, stageDeadline;
    WebSocket origin;
    int stage, lastEmittedStage = -1, baseline, count, menu = -1, completed;
    BlockPos pos;
    BlockState oldBlock;
    Level world;
    RecipeHolder<?> recipe;
    Direction face = Direction.UP;
    float progress;
    boolean collectOnly;
    boolean recoverStation;
    boolean manualRecipe;
    ItemEntity drop;
    Vec3 originPosition;
    JsonObject detail = new JsonObject();

    JsonObject snapshot(String phase) {
        JsonObject r = detail.deepCopy();
        r.addProperty("operation_id", id);
        r.addProperty("kind", kind);
        r.addProperty("phase", phase);
        r.addProperty("stage", stage);
        return r;
    }
}
