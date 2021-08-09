/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package bss;

import java.util.LinkedList;
import java.util.List;
import java.util.Queue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.LinkedBlockingQueue;
import msg.BSSMessage;
import ses.EggyRush;
import ses.TimeStamp;
import ses.VectorClock;

/**
 *
 * @author Creativ
 */
public class BSSOrderModule {

    private BlockingQueue<BSSMessage> WaitPool;
    private BlockingQueue<BSSMessage> PriorWaitPool;
    private VectorClock clock;
    private Release messageRelease;
    private Order order;
    private List<BSSMessage> Buffer;

    public BSSOrderModule() {
        this.WaitPool = new LinkedBlockingQueue<>();
        this.PriorWaitPool = new LinkedBlockingQueue<>();
        this.Buffer = new LinkedList<>();
        this.clock = new VectorClock();
        this.messageRelease = new Release();
        this.order = new Order();


        messageRelease.reference(PriorWaitPool);
        messageRelease.reference(Buffer);
        messageRelease.reference(clock);

        order.reference(Buffer);
        order.reference(WaitPool);
        order.reference(clock);
        order.referencePrior(PriorWaitPool);
    }

    public Queue<BSSMessage> getDeliveryQueue() {
        return this.order.getDeliveryQueue();
    }

    public void start() {
        this.messageRelease.start();
        this.order.start();

    }

    public TimeStamp recordEvent(BSSMessage message) {
        message.setSenderID(EggyRush.myID);
        TimeStamp now = this.clock.tick();
        message.setTimeStamp(now);
        return now;
    }

    public void put(BSSMessage message) {
        synchronized (this.WaitPool) {
            this.WaitPool.add(message);
            this.WaitPool.notify();
        }
    }
}
