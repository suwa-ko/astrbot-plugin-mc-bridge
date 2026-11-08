package com.astrbot.bridge;

import java.util.Set;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.core.BlockPos;
import net.minecraft.world.level.Level;
import net.minecraft.world.level.block.*;
import net.minecraft.world.phys.AABB;

/** Collision-based local standing proof, including partial blocks. No movement or block edits. */
final class TerrainQueries {
    private TerrainQueries(){}
    static int feetBlockY(double feet){return (int)Math.floor(feet+.125);}
    static boolean hazard(Level level,BlockPos pos){
        var s=level.getBlockState(pos);
        return !s.getFluidState().isEmpty()||s.is(Blocks.MAGMA_BLOCK)||s.is(Blocks.CAMPFIRE)||s.is(Blocks.SOUL_CAMPFIRE)||s.is(Blocks.CACTUS)
                ||s.is(Blocks.FIRE)||s.is(Blocks.SOUL_FIRE)||s.is(Blocks.POWDER_SNOW)||s.is(Blocks.SWEET_BERRY_BUSH)||s.is(Blocks.WITHER_ROSE);
    }
    static boolean passable(Level level,BlockPos pos,Set<BlockPos> ignored){
        if(!level.hasChunkAt(pos)||hazard(level,pos))return false;if(ignored.contains(pos))return true;
        var state=level.getBlockState(pos);
        return state.getCollisionShape(level,pos).isEmpty()||state.getBlock() instanceof DoorBlock&&state.getValue(DoorBlock.OPEN)
                ||state.getBlock() instanceof FenceGateBlock&&state.getValue(FenceGateBlock.OPEN);
    }
    static double height(LocalPlayer player,BlockPos cell,Set<BlockPos> ignored){
        Level level=player.level();if(!level.hasChunkAt(cell)||hazard(level,cell)||hazard(level,cell.above()))return Double.NaN;
        for(BlockPos at:new BlockPos[]{cell,cell.above()}){var s=level.getBlockState(at);if(s.getBlock() instanceof DoorBlock&&!s.getValue(DoorBlock.OPEN)||s.getBlock() instanceof FenceGateBlock&&!s.getValue(FenceGateBlock.OPEN))return Double.NaN;}
        for(BlockPos floor:new BlockPos[]{cell,cell.below()}){
            if(ignored.contains(floor)||hazard(level,floor))continue;
            var state=level.getBlockState(floor);var shape=state.getCollisionShape(level,floor);
            if(shape.isEmpty())continue;
            if(!(state.isCollisionShapeFullBlock(level,floor)||state.getBlock() instanceof SlabBlock||state.getBlock() instanceof StairBlock||state.is(Blocks.FARMLAND)||state.is(Blocks.DIRT_PATH)))continue;
            // Sample the central footprint; never use a fence/wall or a narrow decorative collision as a floor.
            double top=Double.NEGATIVE_INFINITY;
            for(var box:shape.toAabbs())if(box.minX<=.25&&box.maxX>=.75&&box.minZ<=.25&&box.maxZ>=.75)top=Math.max(top,box.maxY);
            if(state.getBlock() instanceof StairBlock)top=shape.max(net.minecraft.core.Direction.Axis.Y);
            if(!Double.isFinite(top))continue;
            double feet=floor.getY()+top;
            if(feetBlockY(feet)!=cell.getY())continue;
            AABB body=new AABB(cell.getX()+.21,feet+.001,cell.getZ()+.21,cell.getX()+.79,feet+player.getBbHeight(),cell.getZ()+.79);
            boolean clear=true;
            for(BlockPos at:BlockPos.betweenClosed(BlockPos.containing(body.minX,body.minY,body.minZ),BlockPos.containing(body.maxX,body.maxY,body.maxZ))){
                if(!level.hasChunkAt(at)||hazard(level,at)){clear=false;break;}
                if(!ignored.contains(at)&&level.getBlockState(at).getCollisionShape(level,at).toAabbs().stream().anyMatch(b->b.move(at).intersects(body))){clear=false;break;}
            }
            if(clear)return feet;
        }
        return Double.NaN;
    }
}
