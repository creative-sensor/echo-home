/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package sync;

import java.util.Arrays;
import java.util.Queue;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.logging.Level;
import java.util.logging.Logger;
import msg.Request;
import msg.Token;
import net.MessageTransport;
import ses.EggyRush;

/**
 *
 * @author Creativ
 */
public class SinghalHeuristic extends Thread {

    private MessageTransport messageTransport;
    private int cs;
    private final int REQUEST = 1;
    private final int EXECUTING = 2;
    private final int HOLDING = 3;
    private final int NONE = 0;
    private Queue<Request> requestQueue;
    private int[] SN; // sequence number
    private int[] SV; // state vector
    private Token tok;

    public void reference(MessageTransport mt) {
        this.messageTransport = mt;
    }

    public SinghalHeuristic(int csid) {
        this.cs = csid;
        this.SN = new int[EggyRush.PlayNum];
        this.SV = new int[EggyRush.PlayNum];
        this.requestQueue = new LinkedBlockingQueue<>();

        this.tok = new Token();
        this.tok.setCSID(this.cs);

        for (int i = 0; i < EggyRush.myID; i++) {
            this.SN[i] = 0;
            this.SV[i] = REQUEST;
        }

        for (int i = EggyRush.myID; i < EggyRush.PlayNum; i++) {
            this.SN[i] = 0;
            this.SV[i] = NONE;
        }
        if (EggyRush.myID == 0) {
            this.SV[EggyRush.myID] = HOLDING;
        }
    }

    public void put(Request r) {
        this.requestQueue.add(r);
        synchronized (requestQueue) {
            this.requestQueue.notify();
        }
    }

    public void put(Token t) {
        synchronized (this.tok) {
            this.tok.notify();
            this.tok = t;
        }
    }

    public void request() {
        if (this.SV[EggyRush.myID] != HOLDING) {
            SV[EggyRush.myID] = REQUEST;
            SN[EggyRush.myID]++;
            Request request = new Request(cs, EggyRush.myID, SN[EggyRush.myID]);
            for (int i = 0; i < SV.length; i++) {
                if (i != EggyRush.myID) {
                    if (SV[i] == REQUEST) {
                        messageTransport.send(request, i);
                    }
                }
            }
            try {
                synchronized (this.tok) {
                    this.tok.wait();
                }
            } catch (InterruptedException ex) {
                Logger.getLogger(SinghalHeuristic.class.getName()).log(Level.SEVERE, null, ex);
            }
//            System.out.println("Got Token: sn/sv" + Arrays.toString(this.tok.SequenceNumber())
//                    + "; " + Arrays.toString(this.tok.StateVector()));
            this.SV[EggyRush.myID] = EXECUTING;
        }

    }

    public void printToken() {
        System.out.println("Got TOKEN:CS = " + this.tok.getCSID() + "; SN = " + Arrays.toString(this.tok.SequenceNumber())
                + "; SV = " + Arrays.toString(this.tok.StateVector()));
    }

    public void release() {
        synchronized (this.SV) {
            this.SV[EggyRush.myID] = NONE;
            this.tok.StateVector()[EggyRush.myID] = NONE;
            for (int j = 0; j < this.SV.length; j++) {
                if (this.SN[j] > this.tok.SequenceNumber()[j]) {
                    this.tok.StateVector()[j] = this.SV[j];
                    this.tok.SequenceNumber()[j] = this.SN[j];
                } else {
                    this.SV[j] = this.tok.StateVector()[j];
                    this.SN[j] = this.tok.SequenceNumber()[j];
                }
            }

            boolean allNONE = true;
            for (int j = 0; j < this.SV.length; j++) {
                if (this.SV[j] != NONE) {
                    allNONE = false;
                    break;
                }
            }
            if (allNONE) {
                this.SV[EggyRush.myID] = HOLDING;
            } else {
                for (int i = 0; i < SV.length; i++) {
                    if (SV[i] == REQUEST) {
                        messageTransport.send(tok, i);
                        break;
                    }
                }
            }
        }
        System.out.println("Release TOKEN:CS = " + this.tok.getCSID() + "; SN = " + Arrays.toString(this.tok.SequenceNumber())
                + "; SV = " + Arrays.toString(this.tok.StateVector()));
    }

    @Override
    public void run() {
        // read buffer to get sync request or token
        Request request1;
        while (true) {
            synchronized (requestQueue) {
                if (requestQueue.isEmpty()) {
                    try {
                        requestQueue.wait();
                    } catch (InterruptedException ex) {
                        Logger.getLogger(SinghalHeuristic.class.getName()).log(Level.SEVERE, null, ex);
                    }
                }

            }
            request1 = requestQueue.remove();
            System.out.print(this.cs + ":new request received: " + request1.toString());
            if (this.SN[request1.ID()] < request1.SequenceNum()) {
                switch (this.SV[EggyRush.myID]) {
                    case NONE:
                        this.SV[request1.ID()] = REQUEST;
                        break;
                    case REQUEST:
                        if (SV[request1.ID()] != REQUEST) {
                            this.SV[request1.ID()] = REQUEST;
                            messageTransport.send(new Request(this.cs, EggyRush.myID, this.SN[EggyRush.myID]), request1.ID());
                        }
                        break;
                    case EXECUTING:
                        this.SV[request1.ID()] = REQUEST;
                        break;
                    case HOLDING:
                        this.SV[request1.ID()] = REQUEST;
                        this.tok.StateVector()[request1.ID()] = REQUEST;
                        this.tok.SequenceNumber()[request1.ID()] = request1.SequenceNum();
                        this.SV[EggyRush.myID] = NONE;
                        messageTransport.send(tok, request1.ID());
                        break;
                }
            }

        }

    }
}
