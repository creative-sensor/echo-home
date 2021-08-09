/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package ses;

import java.util.LinkedList;
import java.util.List;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.LinkedBlockingQueue;
import msg.SESMessage;

/**
 *
 * @author Creativ
 */
public class SESOrderModule {

    VectorClock clock;
    PairVector VP;
    MessageOrder messageOrder;
    BufferRelease bufferRelease;
    BlockingQueue<SESMessage> WaitPool;
    BlockingQueue<SESMessage> PriorWaitPool;
    List<SESMessage> Buffer;

    public SESOrderModule() {
        
        this.clock = new VectorClock();
        this.VP = new PairVector();
        this.WaitPool = new LinkedBlockingQueue<>();
        this.PriorWaitPool = new LinkedBlockingQueue<>();
        this.Buffer  = new LinkedList<>();
        
        this.messageOrder = new MessageOrder();
        messageOrder.clock = this.clock;
        messageOrder.WaitPool = this.WaitPool;
        messageOrder.PriorWaitPool = this.PriorWaitPool;
        
        this.bufferRelease = new BufferRelease();
        bufferRelease.Buffer = this.Buffer;
        bufferRelease.PriorWaitPool = this.PriorWaitPool;
        bufferRelease.clock = this.clock;
    }

    public void start(){
        this.messageOrder.start();
        this.bufferRelease.start();
    }
    public void put(SESMessage message) {
        synchronized (WaitPool) {
            this.WaitPool.add(message);
            this.WaitPool.notify();
        }
    }
    
    public void recordEvent(SESMessage message, int PeerID){
        TimeStamp ts = this.clock.tick();
        message.setTimeStamp(ts);
        message.setVM(this.VP);
        log(message, PeerID);
        
    }
    
    private  void log(SESMessage msg, int peerID) {
        synchronized (this.VP) {
            int index = this.VP.hasEntry(peerID);
            if (index == -1) {
                this.VP.add(new Pair(peerID, msg.getTimeStamp().value()));
            } else {
                this.VP.get(index).setTime(msg.getTimeStamp().value());
                //System.out.println("VP = " + this.VP.toString());
            }
        }
    }
}
