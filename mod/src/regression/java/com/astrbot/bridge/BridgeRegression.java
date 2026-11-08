package com.astrbot.bridge;

import com.google.gson.JsonObject;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.net.http.WebSocket;
import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;

/** Headless regressions; no Minecraft instance, server connection or game action. */
public final class BridgeRegression {
    private static void check(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }

    public static void main(String[] args) throws Exception {
        check(BridgeClient.canNormalRespawn(true,true),"dead player may explicitly use normal respawn");
        check(!BridgeClient.canNormalRespawn(false,true)&&!BridgeClient.canNormalRespawn(true,false)&&!BridgeClient.canNormalRespawn(false,false),"alive or missing-death-screen respawn refused");
        check(BridgeClient.shouldAutoRespawn(true,true,true,false),"normal automatic respawn when dead");
        check(!BridgeClient.shouldAutoRespawn(true,false,true,false)&&!BridgeClient.shouldAutoRespawn(true,true,true,true)&&!BridgeClient.shouldAutoRespawn(false,true,true,false),"no alive or duplicate automatic respawn");
        for (String command : List.of("stop", "goto -325 67 -165", "follow player Player", "mine 64 oak_log spruce_log")) {
            check(BridgeClient.validBaritone(command), "Rejected supported command: " + command);
        }
        for (String command : List.of("set allowBreak true", "goto 1 2 3\nstop", "mine 20000 oak_log", "follow player bad#stop", "/op user")) {
            check(!BridgeClient.validBaritone(command), "Accepted unsupported command: " + command);
        }
        Constructor<BridgeClient> constructor = BridgeClient.class.getDeclaredConstructor();
        constructor.setAccessible(true);
        BridgeClient client = constructor.newInstance();
        Field socket = BridgeClient.class.getDeclaredField("socket");
        socket.setAccessible(true);
        FakeSocket old = new FakeSocket();
        FakeSocket current = new FakeSocket();
        socket.set(client, current);
        Method send = BridgeClient.class.getDeclaredMethod("send", JsonObject.class);
        send.setAccessible(true);
        JsonObject first = new JsonObject(); first.addProperty("id", "1");
        JsonObject second = new JsonObject(); second.addProperty("id", "2");
        send.invoke(client, first);
        send.invoke(client, second);
        check(current.sent.size() == 1, "Overlapping asynchronous sends were not serialized");
        current.pending.get(0).complete(current);
        check(current.sent.size() == 2, "Queued message was not sent after first completed");
        current.pending.get(1).complete(current);
        Class<?> listenerType = Class.forName("com.astrbot.bridge.BridgeClient$Listener");
        Constructor<?> listenerConstructor = listenerType.getDeclaredConstructor(BridgeClient.class);
        listenerConstructor.setAccessible(true);
        WebSocket.Listener listener = (WebSocket.Listener) listenerConstructor.newInstance(client);
        listener.onClose(old, 1000, "stale");
        check(socket.get(client) == current, "Old connection close erased current connection");
        listener.onError(old, new IllegalStateException("stale"));
        check(socket.get(client) == current, "Old connection error erased current connection");
        Field http = BridgeClient.class.getDeclaredField("http");
        http.setAccessible(true);
        ((java.net.http.HttpClient) http.get(client)).close();
        System.out.println("PASS: command allowlist, serialized sends, stale connection callbacks");
    }

    private static final class FakeSocket implements WebSocket {
        final List<String> sent = new ArrayList<>();
        final List<CompletableFuture<WebSocket>> pending = new ArrayList<>();
        int active;
        @Override public CompletableFuture<WebSocket> sendText(CharSequence data, boolean last) {
            check(active == 0, "Concurrent sendText call");
            active++;
            sent.add(data.toString());
            CompletableFuture<WebSocket> future = new CompletableFuture<>() {
                @Override public boolean complete(WebSocket value) {
                    active--;
                    return super.complete(value);
                }
            };
            pending.add(future);
            return future;
        }
        @Override public CompletableFuture<WebSocket> sendBinary(ByteBuffer data, boolean last) { throw new UnsupportedOperationException(); }
        @Override public CompletableFuture<WebSocket> sendPing(ByteBuffer data) { throw new UnsupportedOperationException(); }
        @Override public CompletableFuture<WebSocket> sendPong(ByteBuffer data) { throw new UnsupportedOperationException(); }
        @Override public CompletableFuture<WebSocket> sendClose(int code, String reason) { return CompletableFuture.completedFuture(this); }
        @Override public void request(long count) { }
        @Override public String getSubprotocol() { return ""; }
        @Override public boolean isOutputClosed() { return false; }
        @Override public boolean isInputClosed() { return false; }
        @Override public void abort() { }
    }
}
