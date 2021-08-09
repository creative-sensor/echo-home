/*
 * To change this template, choose Tools | Templates
 * and open the template in the editor.
 */
package ai;

import core.Footprint;

/**
 *
 * @author Creativ
 */
public class Cell {
    int r;
    int c;

    public Cell(Footprint fp) {
        this.r = fp.Row();
        this.c = fp.Column();
    }

    
    public Cell(int r, int c) {
        this.r = r;
        this.c = c;
    }
    
    
    public int Row(){
        return this.r;
    }
    
    public int Column(){
        return this.c;
    }
    
    public int getMatrixIndex(){
        return r*32+c;
    }
    
    public boolean isNeighborOf(Cell l){
        if(this.r == l.Row() && (Math.abs(c-l.Column()) == 1)){
            return true;
        }
        
        if(this.c == l.Column() && (Math.abs(r-l.Row()) == 1)){
            return true;
        }
        
        return false;
    }
}
